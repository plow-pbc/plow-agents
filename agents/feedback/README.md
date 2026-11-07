# Feedback agent

A kitchen in one container that triages every company's Plow Feedback room,
files issues, and gets fixes built. [`CLAUDE.md`](CLAUDE.md) is the sous's
role. Boot installs the operator's Claude config from
[code-config](https://github.com/srosro/code-config), clones `SLOTS` checkouts
of each repo in `REPOS` under `~/Hacking/`, and runs the sous in tmux. All
state lives in the `home` volume, so rebuilds and restarts keep it.

## Run it

From this folder, on the host that runs it:

```sh
gh release download -R plow-pbc/swarm -p "swarm-linux-$(uname -m)" -O swarm && chmod +x swarm
mkdir -m 700 secrets && $EDITOR secrets/feedback.env
docker compose up -d --build
```

`secrets/feedback.env` holds:

- `CLAUDE_CODE_OAUTH_TOKEN`: from `claude setup-token` on the Claude account the agent bills to.
- `GH_TOKEN`: a token for the agent's own GitHub identity that can push
  branches and open issues and PRs on the `REPOS`. Don't use a person's token:
  approvals from it would look like theirs.

## Give it Swarm accounts

Each company's server needs its own account, because federation is off. Make one
on each server with a single-use invite link from a member, then bind the home
company's account to the sous and restart:

```sh
docker compose exec feedback swarm accounts add feedback '<invite-link>'   # once per server
docker compose exec feedback bash -c 'k=$(ls ~/.claude-kitchen | grep feedback); mkdir -p ~/.claude-kitchen/$k/swarm \
  && ln -s ~/.swarm/accounts/<home-server>/feedback/credential.json ~/.claude-kitchen/$k/swarm/credential.json'
docker compose restart
```

Give each account a distinct display name. For every other server, hire a relay cook in the
kitchen bound to that server's account: `kitchen hire relay-<company> --swarm-account @feedback:<server>`.

## Watch it

`docker compose exec feedback tmux attach -t feedback` shows the sous; detach with `C-b d`.
