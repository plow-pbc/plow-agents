# plow-agents

Run a Plow agent on your own machine.

A Plow agent is a container that talks to its owner through Plow Chat and
nothing else: no ports, no inbound listener, no dashboard. This repository is
the compose file that boots one, and the tool that mints its credential. It
holds no agent of its own — the image reference says which agent you get.

Nothing here is specific to one image. Any image meeting the Plow agent
contract works: it reads its credential from `/var/lib/plow/credentials`, keeps
everything it owns under `/var/lib/hermes`, and boots its own `Cmd`. That is
also the shape a hosted agent runs in, so what you run here is what a hosted
tenant gets — there is no local mode to drift.

## Run locally

Docker, a phone that can text — and a Plow API that answers
`GET /v1/agents/cloud/me`. The agent is told two lines and asks Plow for the
rest of its identity through that endpoint, so until it ships
([plow-pbc/plow#1666](https://github.com/plow-pbc/plow/pull/1666)) this flow
cannot boot against `api.plow.co`: the agent comes up, asks, is answered
`404`, and refuses to start. A stack that already has the endpoint works today.

```sh
export PLOW_AGENT_IMAGE=public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-<sha>
bin/plow-activate      # prints a code; text it, and it writes ./plow-credentials
docker compose up -d   # boots the agent
```

`plow-activate` starts a Plow activation, prints the code and the number to
text it to, waits for that text, and writes the credential file itself at mode
600. It needs python3 and nothing else. Before writing anything it narrows its
own credential to that agent's line — activation hands back an account-wide one
— so what lands on disk reaches this agent's chats and Plow's inference and
nothing else on the account. If that narrowing does not verify, the credential
is stripped of every grant it has and no file is written. Prompts and progress
go to stderr, and the token is never printed.

That narrowing is not a nicety to skip by pasting a token in by hand. The image
asks Plow who the credential belongs to, and an answer of "not this agent's" —
a `401`, `403` or `404` — is definitive: no retry, no fallback, no boot. A
credential that was never narrowed to a line fails closed by design, rather
than running as something broader than the agent it belongs to.

Then text the number the agent answers on, and it replies. `docker compose logs
-f agent` is what it is doing.

The compose default names a tag that does not exist, so an unset
`PLOW_AGENT_IMAGE` fails on the pull rather than booting some other image. For
Plow's own published agents, the tags are readable from the registry with no
AWS credential at all — it is public:

```sh
token=$(curl -fsSL \
  'https://public.ecr.aws/token/?service=public.ecr.aws&scope=repository:e1h7x4a2/plow-cloud-agents:pull' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -fsSL -H "Authorization: Bearer $token" \
  https://public.ecr.aws/v2/e1h7x4a2/plow-cloud-agents/tags/list
```

Each tag names the commit it was built from. Published tags are `amd64` only
today, so an Apple Silicon Mac runs one under emulation.

### The credential file

`./plow-credentials` is two lines — `PLOW_API_BASE` and `PLOW_AGENT_TOKEN` —
and that is the whole of what the agent is told. Everything else it runs on it
asks Plow for with that credential, or derives: the chat it calls home, the
relay endpoint it offers tools on, its inference key, its server key, its
timezone. A file naming any of those is refused at boot rather than
half-obeyed. `plow-credentials.example` is the shape.

It is mounted read-only, and at `/var/lib/plow/credentials.host` rather than at
the path the agent reads. A bind mount carries this machine's ownership and
mode into the container, and the image will not read a credential file that is
not root's alone — it decides which API the agent's bearer token is sent to. So
the image copies this one into place as root, at 0600, before anything reads
it. Nothing about that needs doing by hand.

Rotation is therefore a rewrite and a restart:

```sh
bin/plow-activate
docker compose restart agent
```

The copy inside the agent's home volume cannot reinstate the token it
replaced. `docker compose down -v` deletes that home volume — the agent's
sessions, memories and provider logins, with no second copy — but **not**
`./plow-credentials`: delete that yourself when you are done, or the next `up`
brings the same agent back.

### Against a Plow stack on this machine

`compose.e2e.yml` puts the agent on that stack's own compose network, so the
API is reached by container name and no host port has to be guessed:

```sh
bin/plow-activate --api-base http://api:8000 --contact-base http://localhost:8000
docker compose -f compose.yml -f compose.e2e.yml up -d
```

The two addresses are one API: `--api-base` is what goes in the file, which the
agent uses from inside the network; `--contact-base` is what this shell can
reach. The activation code arrives at the stack's own inbound-message twin
rather than a phone; deliver it there the way that stack documents.
`scripts/check-activate.sh` does the whole handshake and then checks the
credential it produced — that the file holds those two names at mode 600, and
that the token is narrowed to exactly the line its chat is on.

## Change inference provider

Where inference goes is not part of the agent's identity — it is a decision
about this container — so it lives in `.env` beside the compose file rather
than in the credential:

```sh
printf 'HERMES_PROVIDER=openai-codex\nHERMES_MODEL=gpt-5.5\n' >> .env
docker compose up -d
```

The variable names are the image's, not this repository's. Plow's own agents
are built on [Hermes](https://github.com/NousResearch/hermes-agent), which
takes `HERMES_PROVIDER` and `HERMES_MODEL`; another image may read something
else, and the compose file passes both plus anything else in `.env` straight
through.

For a Hermes image, then: inference goes through Plow by default, on the same
credential as chat, and any provider Hermes supports works instead. Some need a
login first:

```sh
docker compose run --rm agent hermes auth add openai-codex   # device-code login
```

Some take an API key, which is just another line in `.env`:

```sh
printf 'HERMES_PROVIDER=anthropic\nHERMES_MODEL=claude-sonnet-4-5\nANTHROPIC_API_KEY=sk-ant-...\n' >> .env
docker compose up -d
```

`HERMES_MODEL` is not optional when switching, and switching back needs both
lines again: a model id belongs to the provider it was written for, and boot
writes the model only when the variable is set, so dropping the line leaves the
previous provider's id behind rather than restoring a default.

```sh
printf 'HERMES_PROVIDER=plow\nHERMES_MODEL=anthropic/claude-sonnet-5\n' >> .env
docker compose up -d
```

Nothing else has to be restored. The Plow provider's own configuration stays in
place the whole time, and a login written by `hermes auth add` lives in the home
volume and survives a switch away and back.
