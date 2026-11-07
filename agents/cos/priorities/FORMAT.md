# Priorities file format

One markdown file per owner (`so.md`, `mary.md`, ...) in that owner's
**private** data repository (Plow's own: `plow-pbc/priorities`). The format lives
here; the data never does. [`example.md`](example.md) is a filled-in file
with no real data.

A file has a header, the owner's goals, a members table, and one ranked
priorities table. The `## Members` and `## Priorities` headings are optional:

```markdown
# Priorities: <owner handle>

Owner timezone: America/Los_Angeles

## Goals

1. **get-users**: <goal, in the owner's words>
2. **fundraise**: <goal>

## Members

| Member | Kind | Timezone |
|---|---|---|
| @alex:acme.swarm.plow.co | human | America/New_York |
| @builder:acme.swarm.plow.co | agent | |

## Priorities

| Rank | Priority | Supporting goal | Owner (who we ping) | Waiting on | Update expected | Next step (after done) | Cadence | Last nudged | Thread |
|---|---|---|---|---|---|---|---|---|---|
| 1 | <title> | get-users | @alex:acme.swarm.plow.co | <what they owe> | 2026-11-07 | <what happens once it's in> | hourly | 2026-11-06T18:00Z | <Swarm thread link> |
```

- **Rank** orders the table, 1 first. Re-rank by editing; the row order follows it.
- **Goals** are the owner's own, a few at most, each with a short id in the
  owner's words (`get-users`, `cofounder`).
- **Supporting goal** is the id of the goal this row moves forward, or `none`.
  It informs ranking and reporting; the routine doesn't act on it.
- **Owner (who we ping)** is one member: the one the item waits on and the
  one a nudge mentions. `cos` when only the CoS itself is needed.
- **Waiting on** is what that owner owes, in a few words.
- **Update expected** is when an update is due: a date, a UTC time, or blank.
  No nudge goes out before it; see [ROUTINE.md](ROUTINE.md).
- **Next step (after done)** is what happens once the wait is over.
- **Cadence** is `hourly` or `daily`: how often the owner may be nudged.
  Blank means `hourly`.
- **Last nudged** is UTC, `YYYY-MM-DDTHH:MMZ`, blank until the first nudge.
- **Thread** is the item's Swarm thread link.
- A row is removed only once the file's owner has confirmed the item done;
  git history keeps it.
- A literal `|` inside a cell is written `\|`.
- **Members** gives each member's kind and timezone. A human with no
  timezone uses the owner's. Agents have none: quiet hours don't apply to them.
