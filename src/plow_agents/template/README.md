# <your agent>

A Plow cloud agent: a public container image that Plow boots on a VM and gives
a real phone line. This repo is the image.

## What you need

- **[uv](https://docs.astral.sh/uv/)** and **Git**, to install the CLI:
  ```sh
  uv tool install git+https://github.com/plow-pbc/plow-agents
  ```
  It is not on PyPI. That puts `plow-agents` on your `PATH`.
- **Docker**, for building and checking the image.
- **A public registry you can push to** — ghcr.io under this repo is the default. A Docker Hub
  image is written with its host: `docker.io/you/plow-agents`.

You do not need `gh` or a Python of your own.

## Step 1 — Say where this image lives

Open `plow-agents.toml` and fill in both fields:

```toml
slug  = "plow-agents"
image = "ghcr.io/you/plow-agents"
```

`slug` is the name your listing claims on the Agent Index. `image` is the
repository you push to, with no tag.

## Step 2 — Make it yours

`agent.py` is the whole agent. Everything under **your agent** is yours;
everything under **the contract** is what Plow requires and wants no edits.

```python
def compose_reply(body: str, sender: dict, chat: dict) -> str | None:
    """What to say back, or None to stay quiet. This is the part you replace."""
```

Call a model, read a database, do nothing at all — the contract does not care,
as long as the code above `compose_reply` keeps talking to Plow.

It is a starter, not a production agent: it does not deduplicate events or catch up on messages
sent while its socket was down.

## Step 3 — Build and check it

```sh
plow-agents image build
plow-agents image check
```

`image check` runs the image the way exe.dev will: the CMD as PID 1, with
`PLOW_API_BASE` pointing at a stub Plow on this machine and a fake
`PLOW_AGENT_TOKEN`. It fails only on [the contract](#the-contract), and warns
when the agent skips the identity call, the WebSocket or a reply. Passing it is
what makes a deploy worth trying.

## Step 4 — Publish and deploy

```sh
plow-agents image push
plow-agents deploy
plow-agents agents
```

Plow pulls anonymously, so the package must be public. Pushing a `v*` tag runs
the Action, and a package it creates from a public repo comes out public,
linked to the repo. A package created by a local `image push` is private until
you make it public: push once, flip it, push again. The full walkthrough, including the
registry login and the visibility switch, is in the
[plow-agents README](https://github.com/plow-pbc/plow-agents#readme).

`.github/workflows/publish.yml` runs `image build`, `image check` and `image push`
on a `v*` tag. It logs in to ghcr.io only; for any other registry, run `image push` locally.

## Sharp edges

- **Read the environment at run time.** On exe.dev `PLOW_API_BASE` is a proxy
  that adds your token, so `PLOW_AGENT_TOKEN` is not set there. Build every URL
  from `PLOW_API_BASE`, and send the token only when it is set.
- **Ask `/v1/agents/cloud/me` at boot, every boot.** An agent can be moved to
  another line without anything on the VM changing; that call is how you find
  out.
- **No inbound ports.** Nothing dials in. The chat surface is an outbound
  WebSocket.
- **Exit on SIGTERM.** It is the only notice you get before the VM is
  restarted or destroyed.
- **Nothing is baked in.** No tenant, no token, no per-user state — that is
  what lets the image be public and lets Plow pull it with no credential.

## The contract

Three lines, and the only things that fail `image check`:

- Your image's `CMD` is PID 1.
- Your agent reads `PLOW_API_BASE` (no `/v1`) from its environment and talks to it.
- If `PLOW_AGENT_TOKEN` is set, send it as a bearer.

`AGENT_ID`, the listing slug, is set only when the agent was deployed from a
listing. Don't require it.

The authority is [api/cloud-agents/README.md](https://github.com/plow-pbc/plow/blob/main/api/cloud-agents/README.md); this is a restatement. Everything under
**Sharp edges** is advice.
