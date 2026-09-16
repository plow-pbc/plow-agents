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

In your agent repository, beside its Dockerfile, write `plow-agents.toml`.
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

Make the package public in your registry. If the public pull check fails on the first
push, change the package's visibility and run `plow-agents image push` again.
The command prints the image's digest and records `last_pushed` in the project file.

## 5. Request an agent

Replace `ln_xxx` with the free line ID from step 1:

```sh
plow-agents deploy --line ln_xxx
plow-agents agents
```

`deploy` uses the digest recorded by `image push`. You can also pass an
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

This builds first, mints `./plow-credentials`, then starts Compose with `--no-build`.
Compose loads the credential into the container's environment. If startup fails,
the CLI revokes the agent it just minted. When finished:

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
| `plow-agents image push` | Push, verify anonymous access, and record the digest. |
| `plow-agents deploy [TARGET] [--line LINE]` | Request the saved digest, a supplied digest, or an `exe:slug` listing. |
| `plow-agents deploy --local [--line LINE]` | Build, mint, and start Compose locally. |
| `plow-agents agents` | Show tab-separated line, target, and status. |

Every command accepts `--help`. Global `--api-base URL` and `--token-file PATH`
options go before the command.

## Worth knowing

- The registry package must be public; Plow pulls without your registry login.
- Deployments use a digest, not a tag. Multi-architecture indexes are refused by `image push`.
- Builds refuse any `plow-credentials` under the current directory, even if ignored by Docker.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.
"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.
