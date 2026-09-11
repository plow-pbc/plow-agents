"""Run a built image the way exe.dev would, and assert the contract on it.

The contract is in `api/cloud-agents/README.md`. This is that document as a
sequence of assertions, each one named, stopping at the first that fails --
because a container that never read its credential has nothing to say about
whether it would have answered a message.

Nothing here knows about Hermes. An image passes because it satisfies the
contract, not because of what it is built from.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

from .api import die, log
from .docker import Runner, run
from .stub import Stub, host_gateway

# Long enough for a cold container to start a runtime and make one call, short
# enough that a wedged image fails the check rather than the person's patience.
BOOT_TIMEOUT_S = 90
# What `docker stop` allows before SIGKILL. The contract asks for a clean exit
# on SIGTERM, so this is the window the image gets to take one.
STOP_TIMEOUT_S = 10


class ContractError(Exception):
    """One named assertion, and what was seen instead."""

    def __init__(self, assertion: str, saw: str) -> None:
        super().__init__(assertion)
        self.assertion, self.saw = assertion, saw


def check(runner: Runner, *, image: str, agent_id: str, timeout: float = BOOT_TIMEOUT_S) -> list[str]:
    """Every assertion, in order. Returns the ones that passed, or raises `ContractError`."""
    passed: list[str] = []

    def ok(assertion: str) -> None:
        passed.append(assertion)
        log(f"  ok   {assertion}")

    config = _image_config(runner, image)
    if not config.get("Cmd") and not config.get("Entrypoint"):
        raise ContractError("the image has a CMD to run as PID 1", "neither Cmd nor Entrypoint is set")
    ok("the image has a CMD to run as PID 1")

    exposed = config.get("ExposedPorts") or {}
    if exposed:
        raise ContractError("the image declares no listening ports", f"EXPOSE {', '.join(sorted(exposed))}")
    ok("the image declares no listening ports")

    with Stub() as stub, tempfile.TemporaryDirectory() as work:
        container = _start(runner, image=image, stub=stub, agent_id=agent_id, work=work)
        try:
            _await(stub, "identity", timeout, "the agent calls GET /v1/agents/cloud/me with its token",
                   "no identity call arrived")
            ok("the agent calls GET /v1/agents/cloud/me with its token")
            if stub.seen.bad_auth:
                raise ContractError("the agent presents the token from the credentials file",
                                    f"refused: {', '.join(stub.seen.bad_auth[:3])}")
            ok("the agent presents the token from the credentials file")

            # Asked now rather than at start: PID 1 is allowed to be root --
            # it has to be, to read a root-owned 0600 credential -- and the
            # contract is about what the *agent* runs as. The moment it is
            # talking to Plow is the moment that question has an answer.
            uids = _process_uids(runner, container)
            if "10000" not in uids:
                raise ContractError("the agent runs as uid 10000",
                                    f"the only uid(s) running are {', '.join(sorted(uids)) or 'none'}")
            ok("the agent runs as uid 10000")

            _await(stub, "websocket", timeout, "the agent opens the chat WebSocket",
                   "the socket was never opened" if "ticket" in stub.seen.events else "no ticket was minted")
            ok("the agent opens the chat WebSocket")

            if not stub.seen.replied.wait(timeout):
                raise ContractError("the agent replies to one message", "nothing was posted back within the timeout")
            if not stub.seen.reply:
                raise ContractError("the agent replies to one message", "it posted an empty body")
            ok("the agent replies to one message")
            log(f"       it said: {stub.seen.reply[:120]}")

            status = _stop(runner, container)
            if status not in (0, 143):
                raise ContractError("the agent exits cleanly on SIGTERM",
                             "it was killed after the grace period" if status == 137 else f"it exited {status}")
            ok("the agent exits cleanly on SIGTERM")
        finally:
            runner(["docker", "rm", "--force", container], {})
    return passed


def _image_config(runner: Runner, image: str) -> dict:
    stdout = run(runner, ["docker", "image", "inspect", "--format", "{{json .Config}}", image], what=f"inspect of {image}")
    try:
        config = json.loads(stdout)
    except ValueError:
        die(f"docker image inspect {image} did not answer JSON -- build it first")
    if not isinstance(config, dict):
        die(f"docker image inspect {image} did not answer an image config")
    return config


def _start(runner: Runner, *, image: str, stub: Stub, agent_id: str, work: str) -> str:
    """Create the container, drop the credential in as root, and start it.

    `docker cp` rather than a bind mount: Plow writes that file root-owned
    `0600`, and a mount would hand the container a file owned by whoever ran
    the check -- so an image that only works because it can read its own
    credential as uid 10000 would pass here and fail on a real VM.
    """
    created = run(
        runner,
        ["docker", "create", "--add-host", f"{host_gateway()}:host-gateway", image],
        what=f"create from {image}",
    ).strip().splitlines()
    if not created or not created[-1].strip():
        die(f"docker create {image} returned no container id")
    container = created[-1].strip()
    staged = os.path.join(work, "plow")
    os.makedirs(staged, exist_ok=True)
    credentials = os.path.join(staged, "credentials")
    with open(credentials, "w") as handle:
        handle.write(stub.credentials(host_gateway(), agent_id))
    os.chmod(credentials, 0o600)
    run(runner, ["docker", "cp", staged, f"{container}:/var/lib/"], what="writing /var/lib/plow/credentials")
    run(runner, ["docker", "start", container], what=f"start of {container}")
    return container


def _process_uids(runner: Runner, container: str) -> set[str]:
    """Every uid with a process in the container, read from outside it.

    `docker top` runs the host's `ps` against the container's processes, so
    this needs nothing installed in the image -- which matters, because a
    correct agent image may have no shell at all.
    """
    stdout = run(runner, ["docker", "top", container, "-o", "uid,args"], what=f"top of {container}")
    rows = [row.split(None, 1) for row in stdout.splitlines()[1:] if row.strip()]
    return {row[0] for row in rows if row}


def _await(stub: Stub, event: str, timeout: float, assertion: str, saw: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event in stub.seen.events:
            return
        time.sleep(0.05)
    raise ContractError(assertion, saw)


def _stop(runner: Runner, container: str) -> int:
    """SIGTERM, then whatever the container's exit status turned out to be."""
    runner(["docker", "stop", "--timeout", str(STOP_TIMEOUT_S), container], {})
    stdout = run(
        runner,
        ["docker", "inspect", "--format", "{{.State.ExitCode}}", container],
        what=f"inspect of {container}",
    )
    try:
        return int(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        die(f"docker inspect {container} did not answer an exit code")
