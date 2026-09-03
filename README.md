# plow-agents

Run a Plow agent on your own machine, and manage the credentials it needs. A Plow
agent is a container that talks to its owner through Plow Chat and nothing else:
no ports, no inbound listener. This repository is `compose.yml` plus one stdlib
Python script, and no agent of its own: `compose.yml` builds the agent from its
own source repository, so there is nothing to pull and no registry to log into.

## The two credentials

`plow-agents login` stores an **account** token in `~/.config/plow/token` (mode
600). It lists, mints and revokes, so of course it travels — over HTTPS, to Plow,
which is what it is for. What it never does is enter a container: it stays on
this machine and in your own hands. `mint` uses it to write an **agent**
credential to `./plow-credentials`, scoped to one line. That second one is the
only credential a container ever sees.

What that credential carries is Plow's to decide, not this tool's: asking for a
line asks for the assistant role, and it is the same one a cloud agent is minted
with — that line's chats, Plow's inference, `relay:call`, and `payments:request`
with a $200/day cap. Both of those last two reach past the container.
`relay:call` is reach onto the owner's own machine through Latch;
`payments:request` asks for a payment against that cap, which the owner's device
is what consumes. So a container you run yourself holds what a cloud agent
holds. Run one you would grant both to, on a line you are willing to give it.

`login` never asks Plow for an assistant line: being given one changes the
account, and nothing on this machine can tell whether the account about to text
the code already has one. `login --new-line` is how you ask, and it is what an
account with no line yet needs — `login` says so, having looked once it holds a
token. Every later `login` just refreshes that token.

`login` never asks for a phone number, and there is no flag to give it one: the
handset the code is texted *from* is the identity, and the account it binds is
whoever that number already is to Plow. So run it from the phone that should own
this agent, not from whichever one is nearest.

```sh
bin/plow-agents login --new-line   # a brand-new account: get a line too
bin/plow-agents login              # thereafter; prints a code, text it
bin/plow-agents lines          # ln_xxx   Ada   +1 555 0100
bin/plow-agents mint ln_xxx    # writes ./plow-credentials
docker compose up --build
```

`docker compose up --build` is the whole of running one. The first build clones
the agent's source and builds it for this machine's own architecture, which takes
a few minutes; later ones reuse the layers. `PLOW_AGENT_REPO` is what to build —
a git URL with a `#ref`, or a local directory — and it defaults to
`https://github.com/plow-pbc/plow-hermes-agent.git#main`.

Minting again over an existing `./plow-credentials` revokes the key that file
names before it writes the new one, so rotation never leaves a live token nobody
holds a file for. A credential file `mint` cannot read a key id out of stops the
run instead — revoke that one first.

The credential lands relative to the directory you run `mint` from — the one
holding `compose.yml` — and it prints its absolute path. `--out` puts the credential
elsewhere, and then the mount in `compose.yml` has to name that path too.
`--api-base` is a flag on `plow-agents` itself rather than on a verb, so it goes
*before* the verb — `bin/plow-agents --api-base http://127.0.0.1:8000 mint …` —
and it defaults to production, so most runs never pass it. It is the root *this
tool* calls, without `/v1`. `--agent-api-base` is a `mint` flag and goes after
the verb; it is the root written into the credential for the *container* to
call, and defaults to the same one. They differ against a local stack:
`http://127.0.0.1:8000` reaches your API from this machine but is the
container's own loopback inside it. Use
`--agent-api-base http://host.docker.internal:8000`, or join the agent to the
API's compose network and name the API's service instead.

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
docker compose up --build -d
```

## Test a new image

`compose.yml` builds the agent from source. To test an image someone else built,
point compose at it instead — from a scratch directory, so the repo stays clean
and the credential never lands beside your checkout. Copy `compose.yml` there,
then in the copy replace the service's whole `build:` block with an image, rename
the project so a second test cannot share this one's home volume, and pin the
platform if the image is not your machine's architecture:

```yaml
name: agent-test              # was: plow-agent
services:
  agent:
    image: <registry>/<repo>:<tag>
    platform: linux/amd64     # only when the arch differs
```

```sh
# check emulation first, or you will blame the agent for a boot failure
docker run --rm --platform linux/amd64 alpine uname -m   # -> x86_64
docker pull --platform linux/amd64 <registry>/<repo>:<tag>
```

Then the normal flow, run from that scratch directory so the credential lands
beside the `compose.yml` you just copied. `plow-agents` is still the one in your
checkout — `~/plow-agents/bin/plow-agents`, or wherever you cloned it — and
`--api-base` goes before the verb:

```sh
plow-agents login            # prints a code; text it from the phone owning the account
plow-agents lines            # pick a line
plow-agents mint <line-uid>  # writes ./plow-credentials (mode 600)
docker compose up -d         # no --build: the image is already chosen
docker compose logs -f agent # `plow-init: configured ... as cht_...` = the right line
plow-agents revoke           # when done
docker compose down -v
```

Text the line and it answers; there is no "listening" line to wait for, as the
agent binds no port. Use a line you do not mind writing to — it greets the chat
on boot and the history is real.
