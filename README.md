# plow-agents

Give an agent a real Plow line and a real phone number. Two ways to run it: **cloud**, where you
publish a public image and Plow boots it for you, or **self-hosted**, where the container runs on
your own machine and Plow only hands it a credential.

Do the steps yourself, or hand this page to an AI coding agent and let it do most of the work.

## What you need

- **[uv](https://docs.astral.sh/uv/)** — installs and runs the CLI.
  `curl -LsSf https://astral.sh/uv/install.sh | sh`, or `brew install uv`.
- **Git**, which `uv tool install` uses to fetch this repo.
- **A phone that can text** — logging in means texting a code from the phone that owns your Plow account.
- **Docker** — only for the three verbs that build, check and push an image. `login`, `lines`,
  `mint` and `deploy` never touch it.
- **A public registry you can push to** — ghcr.io, Docker Hub, ECR Public, anything. Plow pulls
  anonymously, so the image must be public.

You do not need `gh`, a GitHub CLI login, or a Python of your own.

## Install it

`plow-agents` is not on PyPI. Install it from this repository:

```sh
uv tool install git+https://github.com/plow-pbc/plow-agents
plow-agents --help
```

That puts `plow-agents` on your `PATH`. To move to a newer version later:

```sh
uv tool upgrade plow-agents
```

---

# Cloud: publish an image, let Plow run it

## Step 1 — Log in

`login` prints an activation phrase. Text it, from the phone that owns the account, to the number
it prints. No phone argument is needed. Add `--new-line` if this account holds no assistant line
yet.

```sh
plow-agents login
plow-agents login --new-line    # ... and give me a line to put an agent on
```

The account token lands in `~/.config/plow/token`, mode 600. It stays on this machine.

```sh
plow-agents lines
```

```
LINE	NAME	NUMBER	STATUS
ln_a1b2c3	Ada	+15555550123	free
```

**Checkpoint:** `lines` printed at least one line whose `STATUS` is `free`.

## Step 2 — Start a repo

One repo, one agent. `init` writes a working one: a reference agent, a Dockerfile that satisfies
the contract, a GitHub Action, and `plow-agents.toml` — the file that carries the identity.

```sh
plow-agents init --slug my-agent --image ghcr.io/you/my-agent my-agent
cd my-agent
```

```
agent.py                        the whole agent, ~150 lines, no framework
Dockerfile                      python:3.13-slim, uid 10000, no ports
plow-agents.toml                slug and image
.github/workflows/publish.yml   build, check, push on a v* tag
README.md                       what to edit
```

```toml
slug = "my-agent"
image = "ghcr.io/you/my-agent"
```

`image` is a repository with no tag. Every `image` verb reads this file from the working
directory; `--slug` and `--image` override it for one run without writing to it.

`init` refuses to write over an existing repo — it starts one, it does not merge into one.

## Step 3 — Make it yours

`agent.py` is the agent. One function is the part you replace:

```python
def compose_reply(body: str, sender: dict, chat: dict) -> str | None:
    """What to say back, or None to stay quiet. This is the part you replace."""
```

Everything above it is the contract in
[api/cloud-agents/README.md](https://github.com/plow-pbc/plow/blob/main/api/cloud-agents/README.md):
read `/var/lib/plow/credentials`, drop to uid 10000, call
`GET {PLOW_API_BASE}/v1/agents/cloud/me` on every boot, open the chat WebSocket, answer, and exit
on SIGTERM. Any image that does those things works — the reference agent is one, not the one.

## Step 4 — Build the image

```sh
plow-agents image build
```

exe.dev runs `linux/amd64`, so that is what gets built, whatever your laptop is.

**Checkpoint:** `docker images ghcr.io/you/my-agent` lists the tag.

## Step 5 — Check it against the contract

```sh
plow-agents image check
```

This is the step that saves a failed deploy. It runs your built image the way exe.dev will —
no command override, a credentials file copied in as root at mode 0600, a stub Plow on this
machine — and asserts, in order:

```
  ok   the image has a CMD to run as PID 1
  ok   the image declares no listening ports
  ok   the agent calls GET /v1/agents/cloud/me with its token
  ok   the agent presents the token from the credentials file
  ok   the agent runs as uid 10000
  ok   the agent opens the chat WebSocket
  ok   the agent replies to one message
  ok   the agent exits cleanly on SIGTERM
```

It stops at the first failure and names it, because a container that never read its credential
has nothing to say about whether it would have answered a message:

```
plow-agents: ghcr.io/you/my-agent:latest does not satisfy the contract.
  FAILED: the agent runs as uid 10000
  saw:    the only uid(s) running are 0
```

**Checkpoint:** every assertion passes.

## Step 6 — Log in to the registry

You push with credentials; Plow pulls with none. Both halves have to be true, and the second one
is the step people skip.

On ghcr.io, log in with a personal access token carrying the `write:packages` scope — make one at
<https://github.com/settings/tokens/new?scopes=write:packages>:

```sh
echo "$GHCR_TOKEN" | docker login ghcr.io --username you --password-stdin
```

Docker Hub is `docker login`. ECR Public is:

```sh
aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws
```

**Checkpoint:** `docker login` printed `Login Succeeded`.

## Step 7 — Push it, make it public, and take the digest

```sh
plow-agents image push
```

Three things happen, in order: the tag is pushed; the digest is read back **with no credentials at
all**, which is the same anonymous pull Plow will do; and `last_pushed` is written into
`plow-agents.toml`.

On a first push to ghcr this stops at the second one, because **a brand-new ghcr package is
private**. The package does not exist until you have pushed, so it cannot be made public any
earlier. Do it now:

1. Open `https://github.com/users/you/packages/container/my-agent/settings`
   (an organisation's is `https://github.com/orgs/your-org/packages/container/my-agent/settings`).
2. **Danger Zone → Change visibility → Public.**

On Docker Hub the switch is on the repository's **Settings** tab. ECR Public repositories are
public from the start, and have no such step.

Then run the same command again — it is safe to repeat, and this time it gets all the way through:

```sh
plow-agents image push
```

```
ghcr.io/you/my-agent@sha256:3f0e...c19a
```

```toml
slug = "my-agent"
image = "ghcr.io/you/my-agent"
last_pushed = "sha256:3f0e...c19a"
```

The tag is only a handle for the push. The digest is the reference — it is the only thing Plow
accepts, and the only thing that says which bytes booted.

**Checkpoint:** `last_pushed` is in `plow-agents.toml`, and

```sh
DOCKER_CONFIG=$(mktemp -d) docker manifest inspect ghcr.io/you/my-agent@sha256:3f0e...c19a
```

answers without asking you to log in. That is the whole test: it is the pull Plow does.

## Step 8 — Deploy it on your own line

```sh
plow-agents deploy
plow-agents deploy --line ln_a1b2c3                  # when more than one line is free
plow-agents deploy sha256:9c21...ff04 --line ln_a1b2c3   # some earlier digest
```

With no digest it deploys `last_pushed`. With no `--line` it deploys on your one free line, and
refuses if there is more than one rather than picking.

Plow answers before the machine is built, so `deploy` says *requested* and stops there. `agents`
is what tells you how it ended:

```sh
plow-agents agents
```

```
LINE	SLUG	STATUS	IMAGE
ln_a1b2c3	-	provisioning	ghcr.io/you/my-agent@sha256:3f0e...c19a
```

Run it again until `STATUS` is `running`. A `failed` carries Plow's reason beside it:
`failed (image_pull_timeout)` is a pull that never finished — usually a cold image, sometimes a
private one, so re-check Step 7. `failed (setup_failed)` means the container came up and your
agent did not, which is the image, not the deploy.

**Checkpoint:** `agents` shows `running`, and texting the line's number gets an answer.

---

# Self-hosted: run the container yourself

Plow mints a credential; the container runs wherever you like, and Plow never reaches into it.

```sh
git clone https://github.com/plow-pbc/plow-hermes-agent.git
cd plow-hermes-agent
plow-agents login
plow-agents lines
plow-agents mint ln_a1b2c3
docker compose up --build -d
```

`mint` writes `./plow-credentials`, mode 600. Run it **before** `up`. The first build takes a few
minutes. Watch `docker compose logs -f agent` until `plow-init: configured ... as cht_` appears,
then text the line to talk to it.

Replace the credential without retiring the agent, or retire the agent and take the line back:

```sh
plow-agents rotate            # then recreate the container to load it
plow-agents revoke            # retire the agent named in ./plow-credentials
plow-agents revoke ln_a1b2c3  # retire whatever self-hosted agent holds that line
docker compose down -v
```

To build your own: your repository owns `compose.yml` — copy
[compose.example.yml](compose.example.yml) beside your Dockerfile, start your Dockerfile with
`FROM` the [plow-hermes-agent base image](https://github.com/plow-pbc/plow-hermes-agent), and add
`/plow-credentials` to both `.gitignore` and `.dockerignore`.

---

# Your public profile

`profile` sets the name and photo shown beside your agent. `--photo` takes a local file Plow
hosts, or a public HTTPS URL.

```sh
plow-agents profile --name "Ada" --photo ./ada.png
plow-agents profile --name "Ada" --photo https://example.com/ada.jpg
plow-agents profile --show
```

# The leaderboard

Registering a listing is still a manual step; the CLI does not do it yet. From an agent checkout
with a `./plow-credentials`:

```sh
curl -O https://raw.githubusercontent.com/plow-pbc/agent-index-client/f900ff144076f0a766584b6ec4d0993600779b16/standalone/agent_index_client.py
set -a; . ./plow-credentials; set +a
python3 agent_index_client.py --register --agent "<your-agent-id>" --name "<Agent name>" --blurb "<one line>"
```

Reports appear on the [leaderboard](https://aiworthusing.com/agent-index).

# Sharp edges

- **`image check` serves a stub Plow on every interface** for the length of the run, because the
  container has to reach it. Its token is random per run and dies with the check, but on a shared
  network that port is briefly open.
- **`image check` cannot see inside your image.** It asserts what is observable from outside: the
  declared CMD and ports, which uid the processes run as, what the agent said to Plow, and how it
  exited. An image that passes still has to be right.
- **PID 1 starts as root, and that is correct.** Plow writes the credential root-owned `0600`, so
  an image with `USER 10000` cannot read its own credential. Read it as root and drop privileges
  — the reference agent does it in one function.
- **A tag is never a reference.** Plow refuses anything but `name@sha256:…`. `deploy latest` is an
  error, not a convenience.
- **The image must be public.** `image push` reads the digest back through an empty Docker config
  on purpose. If that step fails, the push worked for *you* and Plow still cannot pull it.
- **Multi-arch builds have no single digest to pin.** `image build` builds `linux/amd64` alone for
  that reason; if you build an index some other way, `image push` will say so.
- **One line, one agent.** `mint` and `deploy` both refuse a line that already answers. Retire the
  agent holding it first — that is a decision, so there is no flag for it.
- **There is no upgrade in place yet.** `deploy` creates an agent; it does not repoint one. A new
  digest on the same line means deleting the running agent first, and `revoke` refuses a cloud
  agent — so today that deletion happens in Plow, and then `image build` → `image push` → `deploy`
  runs again. A verb for it is not in this release.
- **`login` is per-account, not per-agent.** The account token can list lines, mint, rotate,
  revoke and deploy. It never enters a container; only a minted credential does.
- **`down -v` is not the same as `down`.** `down` keeps a self-hosted agent's memory; `down -v`
  starts it fresh and is what picks up image changes to `SOUL.md`.
- **Stale ECR Public credentials 403 on a pull** that should be anonymous. `docker logout
  public.ecr.aws`, then try again.
- **`up` before `mint`** leaves Docker having created `plow-credentials` as a *directory*. Run
  `docker compose down -v && rmdir plow-credentials`, then mint.

# Reference

| Command | Purpose |
| --- | --- |
| `login [--new-line]` | Text a code to log in; optionally be given an assistant line. |
| `lines` | The lines this account holds, and who answers on each. Pick a `free` one. |
| `profile [--name] [--photo] [--show]` | Set or show your public profile. |
| `init [--slug] [--image] [DIR]` | Start an agent repo: reference agent, Dockerfile, Action, toml. |
| `image build [--image] [--tag] [CONTEXT]` | Build for `linux/amd64`, tagged from the toml. |
| `image check [--image] [--tag] [--timeout]` | Run the built image as exe.dev will, and assert the contract. |
| `image push [--image] [--tag]` | Push, verify the anonymous pull, record `last_pushed`. |
| `deploy [DIGEST] [--line] [--image]` | Run a pushed digest on one of your lines. |
| `agents` | What is deployed on this account: line, slug, status, image digest. |
| `mint <line>` | A self-hosted credential for one line, into `./plow-credentials`. |
| `rotate` | Replace that credential. |
| `revoke [line]` | Retire a self-hosted agent, by credential file or by line. |

Every command takes `--help`. `--api-base` points the tool at another Plow; `--token-file` at
another account token. `mint`, `rotate` and `revoke` take `--credential-file <path>`.
The credential file holds `PLOW_API_BASE`, `PLOW_AGENT_TOKEN`, and a `# plow-agent-uid: <uid>`
comment — see [the example](plow-credentials.example).

# Working on this repo

```sh
just                              # lint, then both smoke suites
uv run plow-agents --help         # the working tree, through its own venv
uvx --from . plow-agents --help   # the working tree, built and installed as a package
```

`uvx --from .` resolves the CLI from *this* directory, so it is for working on the CLI itself.
Everywhere else — including inside your own agent repo, which has no CLI package in it — install
it once with `uv tool install git+https://github.com/plow-pbc/plow-agents` and run `plow-agents`.

Neither suite needs Docker, a network, or a Plow account: they drive the real CLI against a local
stub API and a fake docker runner.

# Where changes go

This repo owns the CLI. For sibling responsibilities, see
[plow-hermes-agent's repo map](https://github.com/plow-pbc/plow-hermes-agent#the-repos).

# License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.
"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.
