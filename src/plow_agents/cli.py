"""Manage the Plow agents on your account.

    plow-agents login            one-time: text a code, get an account token
    plow-agents lines            the assistant lines this account holds
    plow-agents mint <line-uid>  that line's credential -> ./plow-credentials
    plow-agents image build      build this repo's image for exe.dev
    plow-agents image push       push it and record the digest
    plow-agents deploy           run that digest on one of your lines
    plow-agents agents           what is deployed on your account
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Annotated

import typer

from . import config, images
from .api import (
    CREDENTIAL_FILE,
    DEFAULT_API_BASE,
    account_lines,
    account_token,
    call,
    checked_api_base,
    credential_agent_uid,
    die,
    install_credential,
    log,
    quote,
    read_credential,
    request,
    strip_v1,
    token_path,
    write_private,
)
from .docker import Runner, subprocess_runner
from .photo import is_hosted_url, read_photo, upload_photo

POLL_S = 3
TIMEOUT_S = 900
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass
class State:
    """What every verb shares: where Plow is, and what this account is holding.

    `runner` is the docker seam. Production passes the subprocess one; the
    tests pass a fake and no daemon is contacted.
    """

    api_base: str = DEFAULT_API_BASE
    token_file: str | None = None
    runner: Runner | None = None

    def token(self) -> str:
        return account_token(self.token_file)

    @property
    def docker(self) -> Runner:
        return self.runner or subprocess_runner


app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__.splitlines()[0])
image_app = typer.Typer(no_args_is_help=True, help="Build and publish this repo's agent image.")
app.add_typer(image_app, name="image")


@app.callback()
def main(
    ctx: typer.Context,
    api_base: Annotated[str, typer.Option("--api-base", help="Plow API root, without /v1")] = DEFAULT_API_BASE,
    token_file: Annotated[str | None, typer.Option("--token-file", help="account token (default ~/.config/plow/token)")] = None,
) -> None:
    ctx.obj = State(api_base=checked_api_base(api_base), token_file=token_file)


def state(ctx: typer.Context) -> State:
    return ctx.obj if isinstance(ctx.obj, State) else State()


# --- the account ------------------------------------------------------------


@app.command()
def login(
    ctx: typer.Context,
    new_line: Annotated[
        bool, typer.Option("--new-line", help="also have Plow give this account a new assistant line and a chat on it")
    ] = False,
) -> None:
    """Text a code, store this account's token."""
    # `provision_chat` asks for a line and a chat on it in the same handshake:
    # a real change to the account, and one nothing here can undo. So it is
    # asked for only when you say so. Nothing on this machine can decide it
    # either -- a cached token is evidence about whoever logged in last, which
    # need not be the account about to text the code below.
    this = state(ctx)
    payload = call("POST", this.api_base, "/v1/auth/activate", body={"name": "plow-agents", "provision_chat": new_line})
    code, secret, send_to = (payload.get(k, "") for k in ("display_code", "activation_secret", "send_to"))
    if not (code and secret and send_to):
        die("activation returned no code, secret or number")
    log("")
    log(f"  Text  Plow Activate: {code}  to  {send_to}")
    log("")
    log("Waiting for that text ...")
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        status, body = request("POST", this.api_base + "/v1/auth/activate/redeem", body={"activation_secret": secret})
        if status == 410:
            die("the code expired -- it is single-use and time-limited; run login again")
        if status // 100 != 2:
            die(f"redeem answered {status} -- run login again for a fresh code")
        if isinstance(body, dict) and body.get("status") == "verified":
            token = body.get("token") or ""
            if not token:
                die("activation verified but returned no token")
            written = write_private(token_path(this.token_file), token + "\n")
            log(f"Wrote {written} (mode 600). This is your ACCOUNT token -- it stays here.")
            # Now that a token is in hand, this account can be asked about
            # itself -- and `mint` has nothing to scope a credential to
            # without a line, so say so here rather than two verbs later.
            if not account_lines(this.api_base, token):
                log("This account holds no assistant line yet: run `plow-agents login --new-line` to be given one.")
            return
        time.sleep(POLL_S)
    die(f"timed out after {TIMEOUT_S}s waiting for the text")


@app.command()
def lines(ctx: typer.Context) -> None:
    """The assistant lines this account holds, and who is already answering on each."""
    this = state(ctx)
    available = account_lines(this.api_base, this.token())
    if not available:
        log("No assistant lines on this account yet.")
        log("Run `plow-agents login --new-line` to be given one.")
        return
    print("LINE\tNAME\tNUMBER\tSTATUS")
    for line in sorted(available, key=lambda line: line["uid"]):
        status = line["agent_uid"] or "free"
        print(f"{line['uid']}\t{line.get('display_name') or ''}\t{line.get('provider_key') or ''}\t{status}")


@app.command()
def profile(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Option("--name", help="public display name")] = None,
    photo: Annotated[str | None, typer.Option("--photo", help="a local image file, or a public https URL")] = None,
    show: Annotated[bool, typer.Option("--show", help="show the current profile")] = False,
) -> None:
    """Set or show this account's public profile."""
    this = state(ctx)
    token = this.token()
    if show:
        if name is not None or photo is not None:
            die("profile --show cannot be combined with --name or --photo")
        print(json.dumps(call("GET", this.api_base, "/v1/auth/profile", token=token), sort_keys=True))
        return
    if name is None and photo is None:
        die("profile needs --name, --photo, or --show")

    # A photo already hosted somewhere public is just a field, so it travels in
    # the same PATCH as the name. A local file is not a field: it is uploaded,
    # and that route takes the name alongside it and writes both as it stores
    # the bytes. Either way this is exactly one request, so a run either
    # happened or it did not -- there is no half of it to be left behind.
    upload = photo is not None and not is_hosted_url(photo)
    content = read_photo(photo) if upload else b""
    if upload:
        result = upload_photo(this.api_base, token, photo, content, name)
    else:
        body = {}
        if name is not None:
            body["display_name"] = name
        if photo is not None:
            body["photo_url"] = photo
        result = call("PATCH", this.api_base, "/v1/auth/profile", token=token, body=body)
    print(json.dumps(result, sort_keys=True))


@app.command()
def mint(
    ctx: typer.Context,
    line: Annotated[str, typer.Argument(help="line uid, as printed by `lines`")],
    credential_file: Annotated[str, typer.Option("--credential-file", help="where to write the credential")] = CREDENTIAL_FILE,
    agent_api_base: Annotated[
        str | None,
        typer.Option(
            "--agent-api-base",
            help="the API root written into the credential, if the container reaches Plow at a different address than this machine does",
        ),
    ] = None,
) -> None:
    """A credential for one line, written to ./plow-credentials."""
    this = state(ctx)
    # `--api-base` is where *this tool* calls; the credential says where the
    # *container* calls. The same address usually, but not against a local
    # stack: a 127.0.0.1 that works out here is the container's own loopback.
    container_base = strip_v1(agent_api_base) if agent_api_base is not None else this.api_base
    destination = os.path.abspath(credential_file)
    if os.path.isdir(destination):
        die(f"{destination} is a directory -- run `docker compose down -v && rmdir {destination}`, then mint again")
    if os.path.exists(destination):
        die(
            f"{destination} already exists -- use `plow-agents rotate` or `plow-agents revoke <line>` to retire the agent; "
            "remove this file manually if its ownership cannot be verified"
        )
    account = this.token()
    # Two agents on one line both answer the same chat and the owner cannot
    # tell which replied, so the line is checked here, before anything is
    # created. There is no flag to proceed anyway: retire the agent that holds
    # it first, which is a decision rather than a keystroke.
    row = next((row for row in call("GET", this.api_base, "/v1/lines", token=account)["data"] if row["uid"] == line), None)
    if row is None:
        die(f"unknown line:{line}")
    if row["agent_uid"]:
        die(f"line:{line} already answers as agent {row['agent_uid']} -- {_remedy(this, account, row['agent_uid'], line)}")
    minted = call("POST", this.api_base, "/v1/agents", token=account,
                  body={"name": "plow-agent", "provider": "self_hosted", "line_uid": line})
    agent, token = minted["agent"], minted["token"]
    if not token:
        die("the self-hosted agent create returned no token")
    if not agent["uid"]:
        die("the self-hosted agent create returned no agent uid")
    try:
        install_credential(destination, agent["uid"], token, container_base)
    except BaseException:
        # A one-time token that never reached its file must not leave a live agent.
        log(f"Install to {destination} failed -- retiring agent {agent['uid']}.")
        call("DELETE", this.api_base, f"/v1/agents/{quote(agent['uid'])}", token=account)
        raise
    log(f"Created agent {agent['uid']} for line:{line}.")


def _remedy(this: State, account: str, agent_uid: str, line: str) -> str:
    """What the holder of an occupied line can actually do about it.

    Which agent decides: `revoke` retires a self-hosted one and refuses every
    other kind, so pointing a cloud agent's owner at it would only cost them a
    second refusal. The lines endpoint publishes no provider, so ask -- on this
    path only, where a request is already being spent to say no.
    """
    status, holder = request("GET", this.api_base + f"/v1/agents/{quote(agent_uid)}", token=account)
    provider = holder.get("provider") if status // 100 == 2 and isinstance(holder, dict) else None
    if provider == "self_hosted":
        return f"retire it with `plow-agents revoke {line}`"
    return "delete it in Plow (it is a cloud agent)" if provider else "retire that agent before minting here"


@app.command()
def rotate(
    ctx: typer.Context,
    credential_file: Annotated[str, typer.Option("--credential-file", help="the credential to rotate")] = CREDENTIAL_FILE,
) -> None:
    """Replace this agent's credential in ./plow-credentials."""
    this = state(ctx)
    path = os.path.abspath(credential_file)
    credential = read_credential(path)
    uid = credential_agent_uid(credential)
    base = strip_v1(credential.get("PLOW_API_BASE", ""))
    rotated = call("POST", this.api_base, f"/v1/agents/{quote(uid)}/credential", token=this.token())
    if not rotated["token"]:
        die("rotation returned no self-hosted token")
    install_credential(path, uid, rotated["token"], base)
    log(f"Rotated agent {uid}. Recreate its container to load the new credential.")


@app.command()
def revoke(
    ctx: typer.Context,
    line: Annotated[str | None, typer.Argument(help="line uid whose self-hosted agent should be retired")] = None,
    credential_file: Annotated[str, typer.Option("--credential-file", help="the credential to revoke")] = CREDENTIAL_FILE,
) -> None:
    """Retire the self-hosted agent in ./plow-credentials, or by line."""
    this = state(ctx)
    path = os.path.abspath(credential_file)
    credential = read_credential(path)
    account = this.token()
    if line:
        available = call("GET", this.api_base, "/v1/lines", token=account)["data"]
        row = next((row for row in available if row["uid"] == line), None)
        if row is None:
            die(f"unknown line:{line}")
        uid = row["agent_uid"]
        if not uid:
            die(f"line:{line} has no agent -- left {path} untouched")
    else:
        uid = credential_agent_uid(credential)
    agent = call("GET", this.api_base, f"/v1/agents/{quote(uid)}", token=account)
    if agent["provider"] != "self_hosted":
        die(f"{uid} is not self-hosted -- delete that agent in Plow")
    status, _ = request("DELETE", this.api_base + f"/v1/agents/{quote(uid)}", token=account)
    if status // 100 != 2 and status != 404:
        die(f"could not retire agent {uid}: HTTP {status}")
    log(f"Retired agent {uid}.")
    if credential.get("agent_uid") != uid:
        if os.path.exists(path):
            log(f"Left {path} in place: it does not name agent {uid}. Remove it manually only after confirming its ownership.")
        return
    if not os.path.exists(path):
        return
    try:
        os.unlink(path)
    except OSError as error:
        log(f"{path} could not be removed ({error}). It names a retired agent; delete it yourself.")
        raise SystemExit(2) from error
    log(f"Removed {path}.")


# --- the image --------------------------------------------------------------


@app.command()
def init(
    slug: Annotated[str, typer.Option("--slug", help="the listing slug this repo claims")] = "",
    image: Annotated[str, typer.Option("--image", help="the public image reference to push to, without a tag")] = "",
    directory: Annotated[str, typer.Option("--directory", help="where to write it")] = ".",
) -> None:
    """Write plow-agents.toml, the one file that says which agent this repo is."""
    path = os.path.abspath(os.path.join(directory, config.CONFIG_FILE))
    if os.path.exists(path):
        die(f"{path} already exists -- edit it, or pass --directory")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(f'slug = "{slug}"\nimage = "{image}"\n')
    log(f"Wrote {path}. Set `slug` and `image` before `plow-agents image push`.")


@image_app.command("build")
def image_build(
    ctx: typer.Context,
    image: Annotated[str | None, typer.Option("--image", help="override the image in plow-agents.toml")] = None,
    tag: Annotated[
        str, typer.Option("--tag", help="the tag to push under; only a handle, the digest is the reference")
    ] = images.DEFAULT_TAG,
    context: Annotated[str, typer.Argument(help="build context")] = ".",
) -> None:
    """Build this repo's image for linux/amd64, tagged from plow-agents.toml."""
    this = state(ctx)
    reference = images.build(this.docker, image=config.load(image=image).need_image(), tag=tag, context=context)
    log(f"Built {reference}.")


@image_app.command("push")
def image_push(
    ctx: typer.Context,
    image: Annotated[str | None, typer.Option("--image", help="override the image in plow-agents.toml")] = None,
    tag: Annotated[
        str, typer.Option("--tag", help="the tag to push under; only a handle, the digest is the reference")
    ] = images.DEFAULT_TAG,
) -> None:
    """Push the image, verify it pulls anonymously, and record the digest."""
    this = state(ctx)
    settings = config.load(image=image)
    digest = images.push(this.docker, image=settings.need_image(), tag=tag)
    config.record_last_pushed(settings.path, digest)
    log(f"Recorded last_pushed in {settings.path}.")
    print(f"{settings.need_image()}@{digest}")


# --- the account's agents ---------------------------------------------------


@app.command()
def deploy(
    ctx: typer.Context,
    digest: Annotated[str | None, typer.Argument(help="sha256:... to deploy (default: last_pushed)")] = None,
    line: Annotated[str | None, typer.Option("--line", help="line uid to deploy on (default: the one free line)")] = None,
    image: Annotated[str | None, typer.Option("--image", help="override the image in plow-agents.toml")] = None,
) -> None:
    """Run a pushed digest on one of your own lines."""
    this = state(ctx)
    settings = config.load(image=image)
    reference = settings.need_image()
    digest = digest or settings.need_last_pushed()
    if not DIGEST.match(digest):
        die(f"{digest} is not a sha256 digest -- Plow deploys digests, never tags")
    account = this.token()
    line = line or _only_free_line(this.api_base, account)
    created = call(
        "POST", this.api_base, "/v1/agents", token=account,
        body={"name": settings.slug or reference.rsplit("/", 1)[-1], "line_uid": line, "provider": f"exe:{reference}@{digest}"},
    )
    agent = created["agent"]
    # Plow answers before the VM is built, so nothing here has seen the agent
    # boot: the phase arrives as `provisioning` and only `agents` can say how
    # it ended. Claiming "deployed" put the failure a poll away from a line
    # that read like success.
    log(f"Requested agent {agent['uid']} on line:{line} ({agent.get('status') or 'provisioning'}).")
    log("Run `plow-agents agents` until it is running.")
    print(f"{agent['uid']}\t{line}\t{reference}@{digest}")


def _only_free_line(api_base: str, account: str) -> str:
    """The line to deploy on when none was named, or a refusal that names the choice."""
    free = [row["uid"] for row in account_lines(api_base, account) if not row["agent_uid"]]
    if not free:
        die("no free line on this account -- run `plow-agents lines`, then retire an agent or `login --new-line`")
    if len(free) > 1:
        die(f"more than one free line -- pass --line with one of: {', '.join(sorted(free))}")
    return free[0]


@app.command()
def agents(ctx: typer.Context) -> None:
    """What is deployed on this account: line, slug, status, and the image digest it booted."""
    this = state(ctx)
    deployed = call("GET", this.api_base, "/v1/agents", token=this.token())
    if not deployed:
        log("Nothing deployed on this account.")
        return
    # Tab-separated, like `lines`: a digest is 71 characters and the whole
    # point of this verb, and a table that fits the terminal ellipsizes it.
    print("LINE\tSLUG\tSTATUS\tIMAGE")
    for agent in sorted(deployed, key=lambda agent: agent["uid"]):
        provider = agent.get("provider") or ""
        # `exe:<slug>` names a listing; `exe:<image>@sha256:...` names an image
        # directly and has no slug to show. Anything else is self-hosted.
        target = provider.removeprefix("exe:") if provider.startswith("exe:") else ""
        slug = target if target and "@" not in target else ("-" if target else provider)
        # This is the only verb that can say an agent never came up. Plow
        # answered `deploy` before the VM existed, so a `failed` here is the
        # first and only place the person sees it -- with the failure category
        # beside it, which is what says whether to retry or fix the image.
        status = agent.get("status") or "-"
        if agent.get("failure_code"):
            status = f"{status} ({agent['failure_code']})"
        print(f"{agent.get('line', {}).get('uid') or '-'}\t{slug}\t{status}\t{agent.get('image') or '-'}")
