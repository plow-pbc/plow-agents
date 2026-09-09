# plow-agents

Run a Plow agent in Docker and give it a real Plow line.

Requires Python 3 (standard library only), Git, and Docker Compose. Install the CLI:

```sh
git clone https://github.com/plow-pbc/plow-agents.git
export PATH="$PWD/plow-agents/bin:$PATH"
```

## Quickstart

The sample base has no Compose file; copy this repo's starter into its checkout:

```sh
git clone https://github.com/plow-pbc/plow-hermes-agent.git
cp plow-agents/compose.example.yml plow-hermes-agent/compose.yml
cd plow-hermes-agent
```

Log in, then text the printed activation phrase from the phone that owns your account. No phone argument is needed.
Use `login --new-line` if you need an assistant line. Replace `ln_xxx` with a free line ID (see `lines` in Reference).

```sh
plow-agents login
plow-agents mint ln_xxx
docker compose up --build -d
```

`mint` writes `./plow-credentials`; run it before `up`. Once the agent starts, text the selected line to talk to it.
When finished, retire the agent and remove its local memory:

```sh
plow-agents revoke
docker compose down -v
```

## Build your own agent

Your repository owns `compose.yml`; copy [compose.example.yml](compose.example.yml) beside your Dockerfile.
Start your Dockerfile with `FROM` the [plow-hermes-agent base image](https://github.com/plow-pbc/plow-hermes-agent); follow its build instructions.
Add `/plow-credentials` to both `.gitignore` and `.dockerignore`.
`docker compose down` keeps memory; `docker compose down -v` starts fresh, including after edits to baked-in agent files.

## Leaderboard

1. Build on this template and the [plow-hermes-agent base](https://github.com/plow-pbc/plow-hermes-agent). See [life-assistant-hermes-agent](https://github.com/plow-pbc/life-assistant-hermes-agent) for a working example.
2. Pick an ID and register it once from your agent checkout. Log in and mint a free line if you have no credential yet:

```sh
plow-agents login
plow-agents mint ln_xxx
curl -O https://raw.githubusercontent.com/plow-pbc/agent-index-client/main/standalone/agent_index_client.py
set -a; . ./plow-credentials; set +a
python3 agent_index_client.py --register --agent "<your-agent-id>" --name "<Agent name>" --blurb "<one line>"
```

3. Bake the reporter into your image: copy the example's [agent-index service](https://github.com/plow-pbc/life-assistant-hermes-agent/tree/main/image/s6-overlay/s6-rc.d/agent-index) and [client installation](https://github.com/plow-pbc/life-assistant-hermes-agent/blob/main/Dockerfile). Set `AGENT_ID=<your-agent-id>` in your own Compose environment, then rebuild and start it. Reports appear on the [leaderboard](https://aiworthusing.com/agent-index).

`profile` is only for the leaderboard. Set your name and photo (a local file Plow hosts, or a public HTTPS URL), or view them:

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
| `profile [--name NAME] [--photo PHOTO] [--show]` | Set or show your leaderboard profile. |
| `mint <line>` | Write a credential for a free line to `./plow-credentials`. |
| `rotate` | Replace the credential; recreate the container to load it. |
| `revoke [line]` | Retire the agent in the credential file, or the self-hosted agent on a line. |

Prefix commands with `plow-agents`; each accepts `--help`. `mint`, `rotate`, and `revoke` accept `--credential-file <path>`.
The credential file contains `PLOW_API_BASE`, `PLOW_AGENT_TOKEN`, and a `# plow-agent-uid: <uid>` comment. See [the example](plow-credentials.example).

## Troubleshooting

A base-image 403 may be stale ECR credentials: try `docker logout public.ecr.aws`, then rebuild.
If `up` ran before `mint`, Docker created a credential directory. Run `docker compose down -v && rmdir plow-credentials`, then mint a line.

## Where changes go

This repo owns the credential CLI and starter Compose file.
For sibling responsibilities, see [plow-hermes-agent's repo map](https://github.com/plow-pbc/plow-hermes-agent#the-repos).

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.
"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.
