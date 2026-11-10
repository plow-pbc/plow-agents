# Priority nudge routine

The CoS runs this on every hourly wake, in each Swarm room the wake names
(company priorities live in `#general`).

1. Read each room's open priorities with the Swarm `priorities` tool
   (`{room}`), then each one's chain and blocking task (`{ref}`). If the read
   fails, log `logger -t cos-priorities "skipped: <error>"` and stop.
2. For each priority, in rank order, take its blocking task, or the priority
   itself when it has no open child:
   1. **Unblock it yourself first.** If you are responsible, or the wait is
      within your reach, do it, then `set_priority` it to what is left.
   2. **Hand off when a task's done-when is met.** Close it per step 3.
      If its parent's done-when still isn't met, create the next task under
      the parent (`set_priority` with `parent`, `responsible` and
      `check_in`), and ask the responsible person in a `send` that names it.
   3. If it still waits on someone else, nudge only when both hold:
      - `check_in` is blank or has passed. A date alone means the end of that
        day in the operator's timezone;
      - the responsible person is an agent, or a human whose local time
        (theirs if you know it, else the operator's) is between 08:00 and
        21:00. Check with `TZ=<zone> date +%H:%M`.
   4. To nudge, `send` in the priority's room, naming its ref and mentioning
      the responsible person, saying what you're waiting on in one or two
      sentences. Then move `check_in` to the next time you'd nudge again
      (an hour out by default, a day out for anything less urgent).
3. Never mark a priority done until its done-when is met: propose "done" to
   whoever is accountable, and set `status: done` after they confirm.

Report one line per nudge or skip to the owner's DM only when something
moved; a wake with nothing due says nothing.
