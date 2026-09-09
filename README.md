# plow-agents

Run a Plow agent in Docker and give it a real Plow line.

Requires Python 3 (standard library only), Git, and Docker Compose. Install the CLI:

```sh
git clone https://github.com/plow-pbc/plow-agents.git
export PATH="$PWD/plow-agents/bin:$PATH"
```

## Quickstart

The sample base ships its own `compose.yml`:

```sh
git clone https://github.com/plow-pbc/plow-hermes-agent.git
cd plow-hermes-agent
```

Log in, then text the printed activation phrase from the phone that owns your account. No phone argument is needed.
Use `login --new-line` if you need an assistant line. Replace `ln_xxx` with a free line ID.

```sh
plow-agents login
plow-agents lines
plow-agents mint ln_xxx
docker compose up --build -d
```

`mint` writes `./plow-credentials`; run it before `up`. The first build takes a few minutes.
Watch `docker compose logs -f agent` until `plow-init: configured ... as cht_` appears, then text the selected line to talk to it.
When finished, retire the agent and remove its local memory:

```sh
plow-agents revoke
docker compose down -v
```

## Build your own agent

Your repository owns `compose.yml`; copy [compose.example.yml](compose.example.yml) beside your Dockerfile.
Start your Dockerfile with `FROM` the [plow-hermes-agent base image](https://github.com/plow-pbc/plow-hermes-agent).
Pulling the published base from public.ecr.aws can 403 on stale credentials: `docker logout public.ecr.aws`, then rebuild.
Add `/plow-credentials` to both `.gitignore` and `.dockerignore`.
`docker compose down` keeps memory; `docker compose down -v` starts fresh and picks up image changes to `SOUL.md`. Skill updates reach the agent on rebuild alone; a skill the agent has changed, and google-workspace, need `down -v`.

## Leaderboard

1. Build on this template and the [plow-hermes-agent base](https://github.com/plow-pbc/plow-hermes-agent). See [life-assistant-hermes-agent](https://github.com/plow-pbc/life-assistant-hermes-agent) for a working example.
2. Pick an ID and register it once from your agent checkout, using the existing `./plow-credentials`:

```sh
curl -O https://raw.githubusercontent.com/plow-pbc/agent-index-client/f900ff144076f0a766584b6ec4d0993600779b16/standalone/agent_index_client.py
set -a; . ./plow-credentials; set +a
python3 agent_index_client.py --register --agent "<your-agent-id>" --name "<Agent name>" --blurb "<one line>"
```

3. The reporter runs as an s6 longrun in your image; see [Building a variant image](https://github.com/plow-pbc/plow-hermes-agent/blob/main/README.md#building-a-variant-image) for how to add one. Copy the [life-assistant agent-index service](https://github.com/plow-pbc/life-assistant-hermes-agent/tree/main/image/s6-overlay/s6-rc.d/agent-index) and its [client installation](https://github.com/plow-pbc/life-assistant-hermes-agent/blob/main/Dockerfile).

Add `AGENT_ID` under `environment` in your agent service in `compose.yml`, set your registered ID, then rebuild and start it. Reports appear on the [leaderboard](https://aiworthusing.com/agent-index).

`profile` sets your account's public name and photo. `--photo` takes a local file Plow hosts, or a public HTTPS URL.

```sh
plow-agents profile --name "Ada" --photo ./ada.png
plow-agents profile --name "Ada" --photo https://example.com/ada.jpg
plow-agents profile --show
```

## Reference

| Command | Purpose |
| --- | --- |
| `login [--new-line]` | Text a code to log in; optionally create an assistant line. |
| `lines` | List line IDs, names, numbers, and status; choose a `free` line. |
| `profile [--name NAME] [--photo PHOTO] [--show]` | Set or show your account profile. |
| `mint <line>` | Write a credential for a free line to `./plow-credentials`. |
| `rotate` | Replace the credential; recreate the container to load it. |
| `revoke [line]` | Retire the agent in the credential file, or the self-hosted agent on a line. |

Prefix commands with `plow-agents`; each accepts `--help`. `mint`, `rotate`, and `revoke` accept `--credential-file <path>`.
The credential file contains `PLOW_API_BASE`, `PLOW_AGENT_TOKEN`, and a `# plow-agent-uid: <uid>` comment. See [the example](plow-credentials.example).

## Troubleshooting

If `up` ran before `mint`, Docker created a credential directory. Run `docker compose down -v && rmdir plow-credentials`, then mint a line.

## Where changes go

This repo owns the credential CLI and starter Compose file.
For sibling responsibilities, see [plow-hermes-agent's repo map](https://github.com/plow-pbc/plow-hermes-agent#the-repos).

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.
"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.
