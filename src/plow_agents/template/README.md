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
- **A public registry you can push to** — ghcr.io under this repo is the default.

You do not need `gh` or a Python of your own.

## Step 1 — Say where this image lives

Open `plow-agents.toml` and fill in both fields:

```toml
slug  = "my-agent"
image = "ghcr.io/you/my-agent"
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
as long as the four things above `compose_reply` keep happening.

## Step 3 — Build and check it

```sh
plow-agents image build
plow-agents image check
```

`image check` runs the image the way exe.dev will: as uid 10000, with the CMD
as PID 1, with a credentials file dropped in as root, against a stub Plow on
this machine. It asserts the contract in order and names the first thing that
fails. Passing it is what makes a deploy worth trying.

## Step 4 — Publish and deploy

```sh
plow-agents image push
plow-agents deploy
plow-agents agents
```

Push once, make the package public — a new ghcr package is private and Plow
pulls anonymously — then push again. The full walkthrough, including the
registry login and the visibility switch, is in the
[plow-agents README](https://github.com/plow-pbc/plow-agents#readme).

`.github/workflows/publish.yml` does the same three commands on a `v*` tag.

## Sharp edges

- **The credentials file is root-owned `0600`, so PID 1 starts as root.** That
  is why the Dockerfile has no `USER` line: an image that declares `USER 10000`
  cannot read its own credential. `agent.py` reads it and then drops to uid
  10000 before touching the network, and `image check` confirms the process
  talking to Plow is 10000 and not root.
- **Ask `/v1/agents/cloud/me` at boot, every boot.** An agent can be moved to
  another line without anything on the VM changing; that call is how you find
  out.
- **No inbound ports.** Nothing dials in. The chat surface is an outbound
  WebSocket, and `image check` fails an image that declares an `EXPOSE`.
- **Exit on SIGTERM.** `docker stop` is how the VM goes away. An image that
  has to be killed fails the check.
- **Nothing is baked in.** No tenant, no token, no per-user state — that is
  what lets the image be public and lets Plow pull it with no credential.

## The contract

The full statement of what an image must do is in
[api/cloud-agents/README.md](https://github.com/plow-pbc/plow/blob/main/api/cloud-agents/README.md).
