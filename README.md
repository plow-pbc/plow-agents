# plow-agents

Run a Plow agent on your own machine, and manage the credentials it needs. A Plow
agent is a container that talks to its owner through Plow Chat and nothing else:
no ports, no inbound listener. This repository is `compose.yml` plus one stdlib
Python script, and no agent of its own — the image says which agent you get, and
any image meeting the contract works.

## The two credentials

`plow-agents login` stores an **account** token in `~/.config/plow/token` (mode
600). It lists, mints and revokes; it is yours and never leaves this machine.
`mint` uses it to write an **agent** credential to `./plow-credentials`, scoped to
one line — that line's chats and Plow's inference, nothing else. That second one
is the only credential a container ever sees.

```sh
bin/plow-agents login          # prints a code; text it
bin/plow-agents lines          # ln_xxx   +1 555 0100
bin/plow-agents mint ln_xxx    # writes ./plow-credentials and ./.env
docker compose up -d
```

`mint` also asks Plow which image runs the agent you named — `--provider hermes`
by default — and writes it to `.env` as `PLOW_AGENT_IMAGE`, where `compose.yml`
reads it. No image is named in this repository on purpose: a tag here would
quietly fall behind the one your Plow boots. They are `amd64` only today, so an
Apple Silicon Mac emulates.

Both files land relative to the directory you run `mint` from — the one holding
`compose.yml` — and it prints their absolute paths. `--out` puts the credential
elsewhere, and then the mount in `compose.yml` has to name that path too.
`--api-base` is the root the *agent* will use, without `/v1`.

Then text the number the agent answers on and it replies; `docker compose logs -f
agent` is what it is doing. When you are done:

```sh
bin/plow-agents revoke         # revokes the key, deletes ./plow-credentials
docker compose down -v         # deletes the agent's home volume
```

## The credential file

```
# plow-agents-key-id: 42
PLOW_API_BASE=https://api.plow.co
PLOW_AGENT_TOKEN=…
```

Those two names are the whole of what the agent is told. Everything else it runs
on it asks Plow for with that credential: the chat it calls home, the relay
endpoint, its inference key, its timezone. A file naming any of those is refused
at boot rather than half-obeyed. The key id is a **comment** on purpose — the
image's parser skips comments, and a third `NAME=value` line would be a refused
boot — and `revoke` is what reads it.

It is mounted read-only at `/var/lib/plow/credentials.host`, not at the path the
agent reads: a bind mount carries this machine's ownership and mode into the
container, and the image will not read a credential file that is not root's alone
— it decides which API the token is sent to. So the image copies it into place as
root at 0600. Rotation is `mint` again plus `docker compose restart agent`.

The home volume is mounted at `${PLOW_AGENT_HOME:-/var/lib/hermes}`. That path
belongs to the **image**: run one that keeps its home elsewhere by setting that
variable, not by editing `compose.yml`.

## Where inference goes

Not the agent's identity but a decision about this container, so it goes in
`.env` beside `compose.yml`. The names belong to the image; Plow's own agents are
built on Hermes. `HERMES_MODEL` is not optional when switching, and switching
back needs both lines: a model id belongs to the provider it was written for.

```sh
printf 'HERMES_PROVIDER=anthropic\nHERMES_MODEL=claude-sonnet-4-5\nANTHROPIC_API_KEY=sk-ant-…\n' >> .env
docker compose up -d
```

`mint` rewrites only that file's `PLOW_AGENT_IMAGE` line, so what you add survives.
