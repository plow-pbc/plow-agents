# plow-agents

Build a Plow agent from a local checkout, run it in Docker, and give it a real
Plow line. The agent opens an outbound connection to Plow; Compose publishes no
ports. This repository is one stdlib-only Python CLI plus a minimal
`compose.example.yml` for new agents.

## Develop an agent locally

Each agent repository owns its `compose.yml`. Put `plow-agents/bin` on `PATH`,
then run the whole loop from the agent checkout so the credential and Compose
project stay together.

```sh
export PATH="/path/to/plow-agents/bin:$PATH"
cd /path/to/your-agent

# For a new agent repository that does not have one yet:
cp /path/to/plow-agents/compose.example.yml compose.yml

# Once on this machine. Add --new-line if this account needs an assistant line.
plow-agents login

plow-agents lines
plow-agents mint ln_xxx           # before the first `up`
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
plow-agents revoke
docker compose down -v
```

The line is now free for the next `mint`. The account token, local checkout,
build cache, and chat history remain; the agent credential and local agent state
do not.

Mint before the first `docker compose up`. If Docker was started first, it
created `./plow-credentials` as an empty directory; recover with: `docker compose down -v && rmdir plow-credentials`. Then mint.

## Credentials and authority

The account token stays on the host and lets this CLI list lines, mint, and
revoke. `mint` writes a mode-600 `./plow-credentials` for the agent repo's
Compose file to mount read-only. Add `/plow-credentials` and `/.env` to that
repo's `.gitignore`. Re-minting over the file rotates its old key rather than
leaving a live credential behind. `plow-credentials.example` shows the file's
shape with placeholder values.

An agent credential is restricted to the chosen line, but it has the same role
as a hosted Plow agent: that line's chats, Plow inference, `relay:call`, and
`payments:request` with a $200/day cap. Relay calls can reach the owner's
machine through Latch, and payment requests can reach the owner's card. Run
only code you trust with both.

## Configuration escape hatches

The included `compose.example.yml` is a minimal `build: .` example, not the
runtime configuration for this repository or every agent. Copy it when starting
an agent repository; that repository then owns its build, environment, mounts,
and project name.

Inference defaults to Plow; set `HERMES_PROVIDER` and `HERMES_MODEL` in `.env`
beside `compose.yml` according to the agent image's provider documentation.

Most developers do not need the remaining CLI flags:

- `--api-base` changes the API called by the CLI and goes before the verb.
- `--token-file` selects a different account-token file.
- `mint --agent-api-base` writes a different API root for the container. This is
  necessary when a local API is `127.0.0.1` on the host but must be reached as
  `host.docker.internal` from Docker.

API roots omit `/v1`; the CLI and agent append it themselves.
