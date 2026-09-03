# plow-agents

Build a Plow agent from a local checkout, run it in Docker, and give it a real
Plow line. The agent opens an outbound connection to Plow; Compose publishes no
ports. This repository is one stdlib-only Python CLI plus `compose.yml`.

## Develop an agent locally

Keep the agent checkout beside this one, then run these commands from the
`plow-agents` directory. `PLOW_AGENT_REPO` must be an absolute path: Compose
resolves relative paths from the project directory, not from the shell that set
the variable.

```sh
cd /path/to/plow-agents

# Once on this machine. Add --new-line if this account needs an assistant line.
bin/plow-agents login

export PLOW_AGENT_REPO="$(cd ../life-assistant-hermes-agent && pwd)"
bin/plow-agents lines
bin/plow-agents mint ln_xxx       # before the first `up`
docker compose up --build -d
docker compose logs -f agent
```

`login` prints `Text  Plow Activate: <code>  to  <number>`. Text that phrase
from the phone that owns the account; the command takes no phone number. It
stores the account token at `~/.config/plow/token`, so later development cycles
skip login.

`lines` labels the line id, name, number, and status:

```text
LINE    NAME    NUMBER         STATUS
ln_xxx  Ada     +1 555 0100    free
```

Status is `free`, `cloud <agent uid>`, or `local <key id>`. `mint` refuses a
line that already has an agent because two agents would answer the same chat.
Use `--force` only when that is intentional.

The first build can take a few minutes. When the log says `plow-init:
configured ... as cht_...`, text the selected line from your phone and the
agent will answer there.

### Edit, rebuild, talk, repeat

The agent's files under `/var/lib/hermes` are copied into its home volume. A
plain rebuild does not replace files already in that volume, so remove the
volume on every source iteration:

```sh
# Edit the local agent checkout, then:
docker compose down -v && docker compose up --build -d
docker compose logs -f agent
```

This keeps `./plow-credentials`, so the rebuilt agent uses the same credential,
line, and chat without another mint. The build cache also remains, so unchanged
layers are reused.

When the development session is over, revoke the credential first and let
Compose remove the container, network, and home volume:

```sh
bin/plow-agents revoke
docker compose down -v
```

The line is now free for the next `mint`. The account token, local checkout,
build cache, and chat history remain; the agent credential and local agent state
do not.

Mint before the first `docker compose up`. If Docker was started first, it
created `./plow-credentials` as an empty directory; recover with: `docker compose down -v && rmdir plow-credentials`. Then mint.

## Credentials and authority

The account token stays on the host and lets this CLI list lines, mint, and
revoke. `mint` writes a mode-600 `./plow-credentials`, which is ignored by Git
and mounted read-only into the container. Re-minting over that file rotates its
old key rather than leaving a live credential behind.

An agent credential is restricted to the chosen line, but it has the same role
as a hosted Plow agent: that line's chats, Plow inference, `relay:call`, and
`payments:request` with a $200/day cap. Relay calls can reach the owner's
machine through Latch, and payment requests can reach the owner's card. Run
only code you trust with both.

## Configuration escape hatches

The default source is
`https://github.com/plow-pbc/plow-hermes-agent.git#main`; setting the absolute
`PLOW_AGENT_REPO` above selects a local checkout, including uncommitted changes.
`PLOW_AGENT_HOME` changes the home path only for an image that uses a different
one. Inference defaults to Plow. A compatible image can read
`HERMES_PROVIDER`, `HERMES_MODEL`, and its provider key from `.env` beside
`compose.yml`.

Most developers do not need the remaining CLI flags:

- `--api-base` changes the API called by the CLI and goes before the verb.
- `--token-file` selects a different account-token file.
- `mint --agent-api-base` writes a different API root for the container. This is
  necessary when a local API is `127.0.0.1` on the host but must be reached as
  `host.docker.internal` from Docker.

API roots omit `/v1`; the CLI and agent append it themselves.
