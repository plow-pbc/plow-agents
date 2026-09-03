# plow-agents

Run a Plow agent on your own machine, and manage the credentials it needs. A Plow
agent is a container that talks to its owner through Plow Chat and nothing else:
no ports, no inbound listener. This repository is `compose.yml` plus one stdlib
Python script, and no agent of its own: `compose.yml` builds the agent from its
own source repository, so there is nothing to pull and no registry to log into.

## Naming the agent

`AGENT_ID` in `.env` names which agent this container is — `AGENT_ID=life`, say.
It is optional: nothing needs it unless the image you build carries something
that publishes under a name, and today that is the usage reporter.

It rides in through `.env` like the other settings, and deliberately has no
shell override: one place to set it is one place to look when it is wrong.

The credential and this answer different questions. The credential says **who**
you are and is a secret; `AGENT_ID` says **which** of your agents this container
is, and is not — a name on a page.

It cannot be worked out from inside. The credential names the line this agent
answers on, not the agent, and Plow answers "cloud agent not found" for one
running on your own machine — so a service that needs it and does not have it
says so and stands down, which is the right failure: a guessed name files this
container's work under somebody else's agent.

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
bin/plow-agents lines          # ln_xxx   Ada   +1 555 0100   free
bin/plow-agents mint ln_xxx    # writes ./plow-credentials
docker compose up --build
```

`docker compose up --build` is the whole of running one. The first build clones
the agent's source and builds it for this machine's own architecture, which takes
a few minutes; later ones reuse the layers. `PLOW_AGENT_REPO` is what to build —
a git URL with a `#ref`, or a local directory — and it defaults to
`https://github.com/plow-pbc/plow-hermes-agent.git#main`.

`lines` ends each row with who is already answering on that line: `free`, or
`cloud <agent uid>` for one Plow runs, or `local <key id>` for one someone
started themselves. It is read off the account's credentials, each of which
names the line it is an assistant on and the cloud agent it belongs to if it has
one — so a credential that resolves to no single line, this tool's own account
token included, holds nothing and makes no line look taken.

`mint` refuses a line that is not `free`, naming who holds it. Two agents on one
line both answer the same texts and the owner cannot tell which replied, so the
usual answer is to revoke the other one first; `--force` mints anyway, for when
that is what you meant. Re-minting the line `./plow-credentials` already holds is
rotation rather than a second agent, and is never refused.

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
bin/plow-agents revoke         # revokes the key and tears the container down
```

## Deleting an agent

`revoke` -- `delete` is the same verb -- is the whole of removing one, and it is
the same delete a hosted agent gets from `DELETE /v1/agents/cloud/{id}`: the
credential is revoked, which deactivates the relay device that credential
carries, and then the container and its home volume are removed. The order is
the cloud's order and for the cloud's reason: the credential goes first, before
anything that can fail is even looked at, so an agent whose teardown errored has
already lost the ability to act. The teardown is `docker compose down -v` in the
directory you run it from, which is where a container the credential belonged to
would be -- named explicitly, `-f ./compose.yml --project-directory .`, with any
`COMPOSE_FILE` and `COMPOSE_PROJECT_NAME` dropped from the environment and
`--env-file` pointed at an empty file, because Compose reads those same two out
of the `.env` this directory is meant to have. What comes down is this
directory's project and never one those happen to name. A teardown that could
not run -- no compose file here, no docker, a `down` that failed, or a
`COMPOSE_PROJECT_NAME` that `up` would have read and so may have started the
agent under another name -- is reported as exactly that, with the command to
finish it by hand, and exits 2: the
credential is revoked and the agent can do nothing, but its container was not
removed and only you can say where it went. What is *not* deleted, here as in the cloud, is the chat on the
line: it and its history are the owner's, and they stay.

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
then in the copy replace the service's whole `build:` block with an image, give
the project a name no other test of yours is using — the home volume is the
project's, so two tests under one name are two agents sharing one home — and pin
the platform if the image is not your machine's architecture. Name the image by
digest, not by tag: a tag is only wherever it was last pushed to point, and this
container is handed a credential carrying `relay:call` and `payments:request`.
Test the bytes you were asked to test.

```yaml
name: agent-test-<yours>      # was: plow-agent; unique per concurrent test
services:
  agent:
    image: <registry>/<repo>@sha256:<digest>
    platform: linux/amd64     # only when the arch differs
```

```sh
# check emulation first, or you will blame the agent for a boot failure
docker run --rm --platform linux/amd64 alpine uname -m   # -> x86_64
docker pull --platform linux/amd64 <registry>/<repo>@sha256:<digest>
```

Then the normal flow, run from that scratch directory so the credential lands
beside the `compose.yml` you just copied. Nothing installs `plow-agents`, and
the scratch directory has no `bin/` — so put the checkout's on `PATH` first, or
spell out its path at every call. `--api-base` goes before the verb:

```sh
export PATH="/path/to/plow-agents/bin:$PATH"   # or call bin/plow-agents by path

plow-agents login            # prints a code; text it from the phone owning the account
plow-agents lines            # pick a line
plow-agents mint <line-uid>  # writes ./plow-credentials (mode 600)
docker compose up -d         # no --build: the image is already chosen
docker compose logs -f agent # `plow-init: configured ... as cht_...` = the right line
plow-agents revoke           # when done: revokes the key, then `down -v` here
```

Text the line and it answers; there is no "listening" line to wait for. The
agent's own API server listens on 127.0.0.1:8642 *inside* the container and
`compose.yml` publishes no port, so nothing of it reaches this machine. Use a
line you do not mind writing to — it greets the chat on boot and the history is
real.

`revoke` says it revoked the key, which is this tool reporting its own success.
Ask Plow instead: any authenticated call carrying that token answers 401 once
the key is gone. Copy the token out of `./plow-credentials` first — `revoke`
deletes the file.

```sh
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer <the token from ./plow-credentials>" \
  https://api.plow.co/v1/chats            # -> 401
```
