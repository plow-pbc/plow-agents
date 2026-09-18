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
plow-agents login
plow-agents lines
```

Text the activation phrase to the number printed by `login`. The `lines` command
shows the phone lines an agent can be deployed on: any account may claim an unheld line. Keep the ID of a
`free` line for step 5. A line held by another account is `in use`; your own
agent is shown by its uid. An older API that does not report availability shows
`unknown` instead of claiming that a line is free.

## 2. Write a Dockerfile

Start with a Dockerfile for your agent. For example, if your Python agent starts
from `agent.py`:

```dockerfile
FROM python:3.11-slim
COPY agent.py /agent.py
CMD ["python", "/agent.py"]
```

Keep credentials out of Git and image builds:

```sh
printf '\n/plow-credentials\n' >> .gitignore
printf '\n/plow-credentials\n' >> .dockerignore
```

## 3. Build the image

```sh
plow-agents image build ghcr.io/YOUR_ACCOUNT/my-agent:v1
```

If ./plow-agents.toml has image = "…", you can omit the name.

## 4. Push the image

Create a [classic GitHub PAT](https://github.com/settings/tokens/new?scopes=write:packages) with `write:packages`; fine-grained PATs cannot push to GHCR. Use the PAT as the password when Docker prompts:

```sh
docker login ghcr.io -u YOUR_GITHUB_USERNAME
plow-agents image push ghcr.io/YOUR_ACCOUNT/my-agent:v1
```

After the first push, make the package public in GitHub package settings; otherwise Plow’s anonymous pull fails.
Copy the full `repository@sha256:…` reference printed on the last line.

## 5. Request an agent

Replace `ln_xxx` with the free line ID from step 1 and use the reference from step 4:

```sh
plow-agents deploy ghcr.io/YOUR_ACCOUNT/my-agent@sha256:… --line ln_xxx
plow-agents agents
```

You can also deploy a listing with `plow-agents deploy exe:hermes --line ln_xxx`.

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

## Publish updates to a listing

Once a Plow admin assigns your account as the listing's owner, inspect both stores:

```sh
plow-agents listing show my-agent
```

This public read needs no token. Its JSON has two labelled halves: **Pin (what Plow boots)**
contains the current Plow digest, enabled state and signup phrases; **Listing (Agent Index)**
contains the site's name, blurb, repository, media, installs and image. Different images stay
visible side by side. Hermes uses its API-provided Index id, `plow-base-hermes`.
A listing absent from the Index is normal: its half is null and stderr says "not on the Agent Index".

Build and push as above, then promote the exact digest from that push in one command:

```sh
plow-agents image push ghcr.io/YOUR_ACCOUNT/my-agent:v2 --promote my-agent
```

The image must be publicly pullable. Plow validates the manifest before accepting the pin.
Promotion affects new agents; it does not replace images on running agents. To promote a digest
you already have, roll back to an older digest, or stop new provisions:

```sh
plow-agents listing promote my-agent ghcr.io/YOUR_ACCOUNT/my-agent@sha256:<64-hex-digest>
plow-agents listing promote my-agent --none
```

Plow is updated first, then the image is mirrored to the Index. Clearing sends null to Plow and
an empty image to the Index. If Plow rejects, the Index is untouched. If the Index fails or drops
the image, stderr reports both outcomes and the command exits nonzero; rerun the same promotion
to retry. If the Index has no listing, or you are a Plow admin but not its Index owner, the pin
still succeeds and the Index is explicitly skipped (exit zero). The CLI does not create Index listings.

Edit site metadata separately; `set` never changes an image:

```sh
plow-agents listing set my-agent --name "My agent" --blurb "What it does" --repo https://github.com/YOUR_ACCOUNT/my-agent
plow-agents listing set my-agent --link https://example.com/start --screenshot https://example.com/demo.png
plow-agents listing set my-agent --video '{"provider":"youtube","id":"VIDEO_ID","title":"Demo"}'
```

These flags update the Index only. `--link` is its installation/tutorial URL; repeat `--screenshot`
to replace the screenshot list. Only supplied flags are sent. Index writes exchange your account
bearer for an Index-only assertion; your account token never goes to the Index.

Plow admins manage signup phrases and ownership with:

```sh
plow-agents listing set my-agent --phrase "Set this up for me: My agent" --owner OWNER_UID --enabled
plow-agents listing set new-agent --name "New agent" --phrase "Set this up for me: New agent" --owner OWNER_UID
```

Repeat `--phrase` to replace the phrase list. `--disabled` stops new provisions. Plow-only flags
never contact the Index. Creating a Plow row also sends its initial `--name` to Plow and prints
"created"; later name edits go only to the Index. Non-admins cannot create Plow rows.

Write commands print the public Plow row as JSON to stdout and progress/outcomes to stderr.
Use `listing show` to read the combined state after site metadata edits. Plain `image push` still
prints a digest reference; with `--promote` it prints the promotion's JSON row.

To target local services (put global flags before the command):

```sh
plow-agents --api-base http://127.0.0.1:19034 --index-base http://127.0.0.1:3847 --token-file /tmp/dev-token listing show my-agent
```

`--index-base` overrides `PLOW_INDEX_BASE`; otherwise the production Index is
`https://tkmx.odio.dev`. HTTPS is required except for the supported local development hosts.

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

## Commands

| Command | Purpose |
| --- | --- |
| `plow-agents login` | Log in by text. |
| `plow-agents lines` | Show the phone lines an agent can be deployed on, with IDs, numbers, and availability. |
| `plow-agents profile [--name NAME] [--photo PHOTO] [--show]` | Set or show your profile; a photo can be a file or HTTPS URL. |
| `plow-agents mint LINE [--credential-file PATH] [--agent-api-base URL]` | Write a credential for a self-hosted agent. |
| `plow-agents rotate [--credential-file PATH]` | Replace the credential; recreate the container to load it. |
| `plow-agents revoke [LINE] [--credential-file PATH]` | Retire any agent on LINE, or the credential-file self-hosted agent. |
| `plow-agents image build [IMAGE]` | Build the current directory for linux/amd64. |
| `plow-agents image push [IMAGE] [--promote SLUG]` | Push and print the digest, or promote that exact digest to a listing. |
| `plow-agents listing show SLUG` | Public combined view of the Plow pin and Agent Index listing. |
| `plow-agents listing promote SLUG REF \| --none` | Set or clear the pin, then mirror it to the Index. |
| `plow-agents listing set SLUG [--name --blurb --repo --video --link --screenshot]` | Edit Index metadata; never the image. |
| `plow-agents listing set SLUG [--phrase --owner --enabled/--disabled]` | Admin-only Plow metadata; repeat --phrase for multiple phrases. |
| `plow-agents deploy TARGET --line LINE` | Request an image@sha256:… or an `exe:slug` listing. |
| `plow-agents deploy --local --line LINE [--agent-api-base URL]` | Mint a credential and start Compose locally. |
| `plow-agents agents` | Show tab-separated line, target, and status. |

Every command accepts `--help`. Global `--api-base URL`, `--index-base URL` and `--token-file PATH`
options go before the command.

## Where changes go

Before editing, find the owner in the [sibling repo map](https://github.com/plow-pbc/plow-hermes-agent/blob/main/README.md#the-repos) and make the change there.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.
"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.
