# Priorities file format

One markdown file per owner (`so.md`, `mary.md`, ...) in that owner's
**private** data repository (Plow's own: `plow-pbc/priorities`). The format lives
here; the data never does. [`example.md`](example.md) is a filled-in file
with no real data.

A file has a header, a members table, and one `##` section per item:

```markdown
# Priorities: <owner handle>

Owner timezone: America/Los_Angeles

| Member | Kind | Timezone |
|---|---|---|
| @alex:acme.swarm.plow.co | human | America/New_York |
| @builder:acme.swarm.plow.co | agent | |

## P1: <title>
- owner: @owner:acme.swarm.plow.co
- blocker: @alex:acme.swarm.plow.co, <what they need to do>
- next step: <one sentence>
- thread: <Swarm thread link>
- status: open
- cadence: hourly
- last_nudged: 2026-11-06T18:00Z
- added_by: @owner:acme.swarm.plow.co
- done_confirmed_by:
```

- **id** is the `P<n>` in the heading. Never reuse one.
- **blocker** is one member, then what they owe. `none` when only the CoS
  itself is needed.
- **status** is `open` or `done`. An item is `done` only once the owner has
  confirmed it; `done_confirmed_by` names who confirmed.
- **cadence** is `hourly` or `daily`: how often the blocker may be nudged.
  Blank or missing means `hourly`.
- **last_nudged** is UTC, `YYYY-MM-DDTHH:MMZ`, blank until the first nudge.
- **Members** gives each blocker's kind and timezone. A human with no
  timezone uses the owner's. Agents have none: quiet hours don't apply to them.
