# plow-agents

Build a Plow agent from a local checkout, run it in Docker, and give it a real
Plow line. The agent opens an outbound connection to Plow; Compose publishes no
ports. This repository is one stdlib-only Python CLI plus a minimal
`compose.example.yml` for new agents.

The images this runs start from
[`plow-pbc/plow-hermes-agent`](https://github.com/plow-pbc/plow-hermes-agent);
[`plow-pbc/life-assistant-hermes-agent`](https://github.com/plow-pbc/life-assistant-hermes-agent)
is the worked variant, and what Plow itself asks of an image is
`api/cloud-agents/README.md` in `plow-pbc/plow`. The base image's README,
"The repos", maps all of them.

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
plow-agents profile --name "Ada" --photo https://example.com/ada.jpg
plow-agents profile --show
plow-agents mint ln_xxx           # before the first `up`
export AGENT_ID=life              # the registered Agent Index id
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

The first build can take a few minutes. When the log says `plow-init:
configured ... as cht_...`, text the selected line from your phone and the
agent will answer there.

`AGENT_ID` says which registered Agent Index page receives this image's usage;
the Life Assistant uses `life`.

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

If `./plow-credentials` was lost, `plow-agents revoke ln_xxx` revokes the one
local credential holding that line. It refuses a cloud agent or an ambiguous
set of holders.

Mint before the first `docker compose up`. If Docker was started first, it
created `./plow-credentials` as an empty directory; recover with: `docker compose down -v && rmdir plow-credentials`. Then mint.

## Credentials and authority

The account token stays on the host and lets this CLI list lines, mint, revoke,
and read or set the public profile. `mint` writes a mode-600 `./plow-credentials` for the agent repo's
Compose file to mount read-only. Add `/plow-credentials` to that repo's
`.gitignore`. Re-minting over the file rotates its old key rather than
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

Inference defaults to Plow; other providers are configured per the agent
image's docs.

Most developers do not need the remaining CLI flags:

- `--api-base` changes the API called by the CLI and goes before the verb.
- `--token-file` selects a different account-token file.
- `mint --agent-api-base` writes a different API root for the container. This is
  necessary when a local API is `127.0.0.1` on the host but must be reached as
  `host.docker.internal` from Docker.

API roots omit `/v1`; the CLI and agent append it themselves.

## Benchmark an image: `bench-cache`

`bench-cache` answers one question with numbers: what does a change to the
agent image cost, or save, per conversation. It was written for the prompt-caching
change (plow-hermes-agent #27) and is not specific to it — a variant is just an
image digest.

One `run` is one variant. Boot an agent from one digest, drive a fixed
conversation through Plow, read the usage rows that conversation produced, tear
the agent down. Two runs of two digests is the before/after, and because
`bench/conversation.json` is data in this repo, the image is the only thing that
differs between them.

```sh
export PATH="/path/to/plow-agents/bin:$PATH"
plow-agents lines                      # pick a line whose STATUS is `free`

bench-cache run --label before --line ln_xxx \
  --image <registry>/plow-hermes-agent@sha256:<digest-before> \
  --api-revision <deployed api sha>

bench-cache run --label after --line ln_xxx \
  --image <registry>/plow-hermes-agent@sha256:<digest-after> \
  --api-revision <deployed api sha>

bench-cache report run-before-*.json run-after-*.json > bench.md
```

Run each variant twice and compare the two before believing either: an agent's
turn time moves with the provider's day.

### What it needs

- **A free line.** `mint` refuses a line that already answers, and this tool
  never passes `--force`. The agent greets the chat on boot and the history is
  real, so use a line you do not mind writing to.
- **The image by digest.** `--image` refuses a tag. A tag is only wherever it
  was last pushed to point, and this container is handed a credential carrying
  `relay:call` and `payments:request`.
- **`plow-ops` on `PATH`** for the usage rows. There is no owner-facing API for
  them: `/v1/usage` aggregates by model, line and day, and never splits prompt
  from completion or carries the cache counters. Without it the run still
  produces its timing table and says the usage half is missing — the record is
  written before the query runs, so a missing binary costs the lookup, not the
  forty minutes of turns. Re-read it later with `bench-cache report`.
- **A `--usage-cmd` whose output this tool has actually parsed.** The default,
  `plow-ops db query {sql}`, is **unverified**: every run so far has gone
  through `psql -A -F,`, whose `(N rows)` footer the parser strips. If
  `plow-ops` frames its CSV differently — a footer of another shape, or none —
  the first prod run is where that shows up. Check the `requests` count against
  the row list before trusting a prod table.
- **The rows are scoped to the run's own credential**, never to the time window
  alone. `mint` prints the session id and the tool reads it back; on a shared
  account — production is one — a window sums every other agent and human
  talking to the model while the run happened. It refuses to query without it.
- **A compose project name no other run of yours is using** (`--project`). The
  home volume belongs to the project, so two runs under one name are two agents
  sharing one home.

### Against a local API

`--api-base` is the API this CLI dials; `--agent-api-base` is what the container
dials, which is a different address when the API is a local compose stack. The
agent also has to be on that stack's network to resolve it, and the turns have to
arrive as inbound messages through that stack's LinQ twin:

```sh
bench-cache run --label before --line ln_p3 \
  --image plow-hermes-agent:bench-before --allow-local-image \
  --api-base http://127.0.0.1:19014 --agent-api-base http://api:8000 \
  --network plow-promptcache_default \
  --twin-base http://127.0.0.1:19015 --from-phone +15551234567 --to-phone +15550000004 \
  --usage-cmd "docker exec -i plow-promptcache-db-1 psql -U plow -d plow -A -F, -c {sql}"
```

`--to-phone` is the line's own number and is not optional: it picks which line
the inbound lands on, and a message on another line is one this agent never
sees. Omit it and the run is a silent no-op — every turn times out, no
completion is made, and the usage table stays empty while the agent looks
perfectly healthy.

Seeding an account on a local stack needs no phone: `POST /v1/auth/activate`
with `provision_chat: true`, post the returned code to the twin's `/ui/inbound`
as `Plow Activate: <code>`, then poll `POST /v1/auth/activate/redeem` for the
token. Write that token to a file and pass `--token-file` — the lines themselves
are seeded by the API's own migration from `CHAT_LINE_PHONES`.

### What it does not measure

- **Cache counters, until the API records them.** `cache_read_tokens` and
  `cache_write_tokens` arrive on `llm_usage` with plow **#1710**. Before that
  build the columns do not exist, and the tool says so in the report header
  rather than printing zeros — "cached nothing" and "did not measure" are the
  two readings a benchmark must never confuse. It reads the live column list at
  run time, so it starts reporting them the boot after #1710 deploys, with no
  change here.
- **A trustworthy cost for a caching variant, on that same older build.** That
  build prices a streaming request off `prompt_tokens` alone, which litellm has
  already folded cache reads into — so a cache read is billed at the full input
  rate and `cost_usd` for a caching variant reads as no saving at all. #1710
  fixes that too. Before it, run the `before` variant only.
- **Ground truth.** `cost_usd` is litellm's cost map times Plow's markup, not an
  invoice. Cross-check a run against the provider's own usage for the same
  window before quoting a saving to anyone.
- **The deployed API build.** There is no version endpoint; `--api-revision` is
  what you pass in, and the header says `unverified` when you don't. Read it
  from the running service:

```sh
plow-ops aws ecs describe-services --cluster plow-prod --services plow-prod-api \
  | jq -r '.services[0].taskDefinition'
plow-ops aws ecs describe-task-definition --task-definition plow-prod-api:<n> \
  | jq -r '.taskDefinition.containerDefinitions[].image'
```

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.

"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.
