# CoS template

What every chief-of-staff (CoS) kitchen runs.

## Priority nudges

A CoS keeps the owner's ranked priorities moving: they are Swarm priority
records, and once an hour the CoS unblocks what it can and nudges whoever
holds up the rest.

- [`priorities/ROUTINE.md`](priorities/ROUTINE.md): what the CoS does on each wake.
- `wake.py` and `systemd/`: the hourly wake. It writes one line into the CoS
  kitchen's socket, the same way kitchen cooks report back.

Install for a CoS kitchen (first edit the socket, room and `ExecStart` paths in the service):

```sh
cp agents/cos/systemd/cos-priorities.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cos-priorities.timer
systemctl --user start cos-priorities.service   # one wake now
```
