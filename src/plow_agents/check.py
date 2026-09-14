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
# The contract's uid. Every process but PID 1 has to be it.
AGENT_UID = "10000"
# `st` in /proc/net/tcp: the kernel's TCP_LISTEN.
TCP_LISTEN = "0A"


class ContractError(Exception):
    """One named assertion, and what was seen instead."""

    def __init__(self, assertion: str, saw: str) -> None:
        super().__init__(assertion)
        self.assertion, self.saw = assertion, saw


def check(runner: Runner, *, image: str, agent_id: str, timeout: float = BOOT_TIMEOUT_S,
          stop_timeout: float = STOP_TIMEOUT_S) -> list[str]:
    """Every assertion, in order. Returns the ones that passed, or raises `ContractError`."""
    passed: list[str] = []

    def ok(assertion: str) -> None:
        passed.append(assertion)
        log(f"  ok   {assertion}")

    config = _image_config(runner, image)
    if not config.get("Cmd") and not config.get("Entrypoint"):
        raise ContractError("the image has a CMD to run as PID 1", "neither Cmd nor Entrypoint is set")
    ok("the image has a CMD to run as PID 1")

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

            _await(stub, "websocket", timeout, "the agent opens the chat WebSocket",
                   "the socket was never opened" if "ticket" in stub.seen.events else "no ticket was minted")
            ok("the agent opens the chat WebSocket")

            # Both asked now, while the agent is connected, because that is the
            # moment the questions have answers. PID 1 is allowed to be root --
            # it has to be, to read a root-owned 0600 credential -- so what is
            # judged is everything else. And a port is judged by what is
            # listening, not by what the image's metadata says: EXPOSE neither
            # opens a socket nor is needed to.
            strays = _non_agent_processes(runner, container)
            if strays:
                raise ContractError(f"every process but PID 1 runs as uid {AGENT_UID}", "; ".join(strays[:3]))
            ok(f"every process but PID 1 runs as uid {AGENT_UID}")

            listening = _listening_ports(runner, container)
            if listening:
                raise ContractError("the agent listens on no port", f"listening on {', '.join(listening[:5])}")
            ok("the agent listens on no port")

            if not stub.seen.replied.wait(timeout):
                raise ContractError("the agent replies to one message", "nothing was posted back within the timeout")
            if not stub.seen.reply:
                raise ContractError("the agent replies to one message", "it posted an empty body")
            ok("the agent replies to one message")
            log(f"       it said: {stub.seen.reply[:120]}")

            _stop(runner, container, stop_timeout)
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


def _non_agent_processes(runner: Runner, container: str) -> list[str]:
    """Every process other than PID 1 not running as the agent's uid, described.

    `docker top` runs the host's `ps` against the container's processes, so
    this needs nothing installed in the image -- which matters, because a
    correct agent image may have no shell at all. Its pids are the host's, so
    PID 1 is found by asking docker which host pid it is.

    No attempt to work out which process holds the socket to Plow: a root
    process that hands the talking to a uid-10000 child is still a root
    process with the credential and the network, and the only process the
    contract lets be root is the one that has to read the file.
    """
    init = _inspect(runner, container, "{{.State.Pid}}").strip()
    stdout = run(runner, ["docker", "top", container, "-o", "pid,uid,args"], what=f"top of {container}")
    rows = [row.split(None, 2) for row in stdout.splitlines()[1:] if row.strip()]
    if not rows:
        return ["docker top listed no processes at all"]
    strays = [f"pid {row[0]} is uid {row[1]}: {row[2] if len(row) > 2 else '?'}"
              for row in rows if len(row) >= 2 and row[0] != init and row[1] != AGENT_UID]
    if not strays and not any(len(row) >= 2 and row[1] == AGENT_UID for row in rows):
        return [f"no process runs as uid {AGENT_UID} -- the only one is PID 1, as uid {rows[0][1]}"]
    return strays


def _listening_ports(runner: Runner, container: str) -> list[str]:
    """Every TCP socket in LISTEN inside the container's network namespace.

    Read from /proc/net/tcp{,6} by a `cat` run inside the container, since that
    table is per network namespace and the host's says nothing about this one.
    An image with no `cat` cannot be checked this way, and "could not look" is
    reported as a failure rather than passed.
    """
    status, stdout = runner(["docker", "exec", container, "cat", "/proc/net/tcp", "/proc/net/tcp6"], {})
    # `cat` exits 1 when a file is missing -- a kernel with no IPv6 has no
    # tcp6 -- and still prints the one it could read. 126/127 is exec failing
    # to find or run `cat` at all.
    if status not in (0, 1) or "local_address" not in stdout:
        raise ContractError("the agent listens on no port",
                            f"could not verify: `docker exec {container} cat /proc/net/tcp` exited {status} "
                            "-- the image needs a `cat` for this check to read its sockets")
    ports = []
    for row in stdout.splitlines():
        fields = row.split()
        if len(fields) > 3 and fields[0].endswith(":") and fields[3] == TCP_LISTEN:
            _, _, port = fields[1].rpartition(":")
            ports.append(f"tcp port {int(port, 16)}")
    return sorted(set(ports))


def _await(stub: Stub, event: str, timeout: float, assertion: str, saw: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event in stub.seen.events:
            return
        time.sleep(0.05)
    raise ContractError(assertion, saw)


def _inspect(runner: Runner, container: str, template: str) -> str:
    return run(runner, ["docker", "inspect", "--format", template, container], what=f"inspect of {container}")


def _state(runner: Runner, container: str) -> tuple[str, int]:
    """(`running`/`exited`/..., exit code) as docker has them now."""
    fields = _inspect(runner, container, "{{.State.Status}} {{.State.ExitCode}}").split()
    try:
        return fields[0], int(fields[1])
    except (ValueError, IndexError):
        die(f"docker inspect {container} did not answer a state and an exit code")


def _stop(runner: Runner, container: str, deadline_s: float) -> None:
    """SIGTERM a running container and require it to be gone, cleanly, in time.

    `docker kill -s TERM` rather than `docker stop`: stop escalates to SIGKILL
    itself and exits 0 either way, which leaves telling "it exited" from "it
    was killed" to an exit code. Here the check owns the deadline.
    """
    assertion = "the agent exits cleanly on SIGTERM"
    status, code = _state(runner, container)
    if status != "running":
        raise ContractError(assertion, f"it had already stopped before SIGTERM was sent ({status}, exit {code})")
    sent, _ = runner(["docker", "kill", "--signal", "TERM", container], {})
    if sent != 0:
        raise ContractError(assertion, f"`docker kill --signal TERM` failed, exit {sent}")
    until = time.monotonic() + deadline_s
    while status == "running" and time.monotonic() < until:
        time.sleep(0.1)
        status, code = _state(runner, container)
    if status == "running":
        raise ContractError(assertion, f"it was still running {deadline_s:g}s after SIGTERM")
    # 143 is 128 + SIGTERM: a process that takes the default action on the
    # signal has exited because of it, which is what was asked.
    if code not in (0, 143):
        raise ContractError(assertion, f"it exited {code}")
