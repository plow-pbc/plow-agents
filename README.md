# plow-agents

Build an agent image, run it on Plow, and text it from your phone.

## What you need

Python 3.11 or newer, Git, Docker, a registry account that can publish public images,
and a phone that can send an activation text. For local runs, use Docker Compose
2.24 or newer and an agent repository with a Dockerfile and `compose.yml`.

Your agent reads `PLOW_API_BASE` from its environment; if `PLOW_AGENT_TOKEN` is set,
send it as a bearer. See the [image contract](https://github.com/plow-pbc/plow/blob/main/api/cloud-agents/README.md).

## 1. Install and log in

```sh
git clone https://github.com/plow-pbc/plow-agents.git
export PATH="$PWD/plow-agents/bin:$PATH"
plow-agents login --new-line
plow-agents lines
```

Text the activation phrase to the number printed by `login`. Use `plow-agents login`
if you already have a line. Keep the ID of a `free` line for step 5.

## 2. Write the project file

Start with a Dockerfile for your agent. For example, if your Python agent starts
from `agent.py`:

```dockerfile
FROM python:3.11-slim
COPY agent.py /agent.py
CMD ["python", "/agent.py"]
```

Beside the Dockerfile, write `plow-agents.toml`.
Replace `YOUR_ACCOUNT` with your registry account and `my-agent` with your agent's name.

```sh
cat > plow-agents.toml <<'TOML'
slug = "my-agent"
image = "ghcr.io/YOUR_ACCOUNT/my-agent:v1"
TOML
printf '\n/plow-credentials\n' >> .gitignore
printf '\n/plow-credentials\n' >> .dockerignore
```

## 3. Build the image

```sh
plow-agents image build
```

This builds the current directory for `linux/amd64`, using the image tag in the project file.

## 4. Push the image

```sh
docker login ghcr.io
plow-agents image push
```

The command prints the full `repository@sha256:…` reference and records it as
`last_pushed` in the project file.

## 5. Request an agent

Replace `ln_xxx` with the free line ID from step 1:

```sh
plow-agents deploy --line ln_xxx
plow-agents agents
```

`deploy` uses the image reference recorded by `image push`. You can also pass an
`image@sha256:…` reference or a `sha256:…` digest of the configured image.
If your account has exactly one free line, `plow-agents deploy` selects it.

## 6. Text it

Run `plow-agents agents` until the status is `running`. Text the number shown by
`plow-agents lines`. A `failed` status includes the failure code beside it.

## 7. Run it locally

Copy [compose.example.yml](compose.example.yml) into your agent repository as
`compose.yml`, then choose a free line:

```sh
curl -fsSL https://raw.githubusercontent.com/plow-pbc/plow-agents/main/compose.example.yml -o compose.yml
plow-agents lines
plow-agents deploy --local --line ln_xxx
docker compose logs -f
```

If the container reaches your API at a different address, pass
`--agent-api-base URL` to `plow-agents deploy --local`. When finished:

```sh
plow-agents revoke
docker compose down -v
```

## Commands

| Command | Purpose |
| --- | --- |
| `plow-agents login [--new-line]` | Log in by text; optionally create a line. |
| `plow-agents lines` | Show line IDs, numbers, and occupancy. |
| `plow-agents profile [--name NAME] [--photo PHOTO] [--show]` | Set or show your profile; a photo can be a file or HTTPS URL. |
| `plow-agents mint LINE [--credential-file PATH] [--agent-api-base URL]` | Write a credential for a self-hosted agent. |
| `plow-agents rotate [--credential-file PATH]` | Replace the credential; recreate the container to load it. |
| `plow-agents revoke [LINE] [--credential-file PATH]` | Retire a self-hosted agent. |
| `plow-agents image build` | Build the current directory for linux/amd64. |
| `plow-agents image push` | Push and record the full image reference. |
| `plow-agents deploy [TARGET] [--line LINE]` | Request the saved digest, a supplied digest, or an `exe:slug` listing. |
| `plow-agents deploy --local [--line LINE] [--agent-api-base URL]` | Build, mint, and start Compose locally. |
| `plow-agents agents` | Show tab-separated line, target, and status. |

Every command accepts `--help`. Global `--api-base URL` and `--token-file PATH`
options go before the command.

## Worth knowing

- The registry package must be public; private images fail to deploy with `failed(pull_failed)`.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.
"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.
