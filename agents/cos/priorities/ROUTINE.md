# Priority nudge routine

The CoS runs this on every hourly wake. The file follows [FORMAT.md](FORMAT.md).

1. `git pull` the private data repository, then read the owner's file.
2. For each `open` item:
   1. **Unblock it yourself first.** If the next step is within your reach,
      do it, then update `blocker` and `next step` to what is left. If a
      reply in the item's thread changed the blocker, record that too.
   2. If it still waits on someone else, nudge only when both hold:
      - `last_nudged` is blank or at least 60 minutes ago (check with
        `date -u`);
      - the blocker is an agent, or a human whose local time (their
        timezone from Members, else the owner's) is between 08:00 and 21:00.
        Check with `TZ=<zone> date +%H:%M`.
   3. To nudge, reply in the item's thread with `swarm send` (use its
      `--thread` flag), mentioning the blocker and giving the next step in
      one or two sentences. Set `last_nudged` to now, UTC.
3. Never set `status: done` yourself. Propose it to the owner; close it only
   after they confirm.
4. If anything changed, commit the file (`priorities: <ids> nudged/updated`)
   and push. Report one line per nudge or skip to the owner's DM only when
   something moved; a wake with nothing due says nothing.
