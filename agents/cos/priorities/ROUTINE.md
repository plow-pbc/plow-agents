# Priority nudge routine

The CoS runs this on every hourly wake. The file follows [FORMAT.md](FORMAT.md).

1. `git pull` the private data repository, then read the owner's file.
2. For each row, in rank order:
   1. **Unblock it yourself first.** If the owner is `cos`, or the wait is
      within your reach, do it, then update the row to what is left. If a
      reply in the item's thread changed who or what it waits on, record that too.
   2. If it still waits on someone else, nudge only when all three hold:
      - `Update expected` is blank or has passed. A date alone means the end
        of that day in the file owner's timezone;
      - `Last nudged` is blank or its `Cadence` interval has passed: 60
        minutes for `hourly` (the default), 24 hours for `daily` (check
        with `date -u`);
      - the owner is an agent, or a human whose local time (their
        timezone from Members, else the file owner's) is between 08:00 and
        21:00. Check with `TZ=<zone> date +%H:%M`.
   3. To nudge, reply in the row's `Thread` with `swarm send` (use its
      `--thread` flag), mentioning the owner and saying what you're waiting
      on and which goal it serves (unless `none`) in one or two sentences.
      Set `Last nudged` to now, UTC.
3. Never remove a row yourself. Propose "done" to the file's owner; remove
   the row only after they confirm.
4. If anything changed, commit the file (`priorities: rank <n> nudged/updated`)
   and push. Report one line per nudge or skip to the owner's DM only when
   something moved; a wake with nothing due says nothing.
