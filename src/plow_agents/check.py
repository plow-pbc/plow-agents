"""Run a built image the way exe.dev would, and check it against the contract.

The contract is three lines, stated in `api/cloud-agents/README.md`: the
image's CMD is PID 1, Plow writes /var/lib/plow/credentials, and the agent uses
it to talk to the Plow API. So two things fail this check -- the image has a
CMD to start, and something inside calls the stub with the token from the
file. Everything else that document recommends is printed as a warning, and
never fails it.

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
# Once the token has been used, how long the agent gets to open the socket and
# answer before the advice is judged on what it has done so far.
ADVICE_WAIT_S = 15
# How long a SIGTERM gets before the exit is called unclean -- `docker stop`'s
# own grace period, which is what the VM gets.
STOP_TIMEOUT_S = 10
# The uid the advice asks every process but PID 1 to run as.
AGENT_UID = "10000"
TOKEN_ASSERTION = "something inside calls the Plow API with the token from the credentials file"


class ContractError(Exception):
    """One named assertion, and what was seen instead."""

    def __init__(self, assertion: str, saw: str) -> None:
        super().__init__(assertion)
        self.assertion, self.saw = assertion, saw


def check(runner: Runner, *, image: str, timeout: float = BOOT_TIMEOUT_S,
          advice_wait: float = ADVICE_WAIT_S, stop_timeout: float = STOP_TIMEOUT_S) -> tuple[list[str], list[str]]:
    """The two assertions, then the advice. Returns (passed, warned), or raises `ContractError`."""
    passed: list[str] = []
    warned: list[str] = []

    def ok(assertion: str) -> None:
        passed.append(assertion)
        log(f"  ok   {assertion}")

    def advise(advice: str, problem: str | None) -> None:
        if problem is None:
            log(f"  ok   {advice}")
        else:
            warned.append(advice)
            log(f"  warn {advice}\n       {problem}")

    config = _image_config(runner, image)
    if not config.get("Cmd") and not config.get("Entrypoint"):
        raise ContractError("the image has a CMD to run as PID 1", "neither Cmd nor Entrypoint is set")
    ok("the image has a CMD to run as PID 1")

    with Stub() as stub, tempfile.TemporaryDirectory() as work:
        container = _start(runner, image=image, stub=stub, work=work)
        try:
            seen = stub.seen
            if not seen.token.wait(timeout):
                raise ContractError(TOKEN_ASSERTION, f"requests arrived without it: {', '.join(seen.bad_auth[:3])}"
                                    if seen.bad_auth else "no request reached the API")
            ok(TOKEN_ASSERTION)

            seen.replied.wait(advice_wait)
            advise("it calls GET /v1/agents/cloud/me on boot", None if "identity" in seen.events
                   else "it never asked -- an agent moved to another line would not find out")
            advise("it opens the chat WebSocket", None if "websocket" in seen.events
                   else "a ticket was minted but the socket never opened" if "ticket" in seen.events
                   else "no ticket was minted")
            advise("it replies to a message", None if seen.reply
                   else "it posted an empty body" if seen.replied.is_set() else "nothing was posted back")
            if seen.reply:
                log(f"       it said: {seen.reply[:120]}")
            advise(f"every process but PID 1 runs as uid {AGENT_UID}", _stray_processes(runner, container))
            advise("it exits cleanly on SIGTERM", _stop(runner, container, stop_timeout))
        finally:
            runner(["docker", "rm", "--force", container], {})
    return passed, warned


def _image_config(runner: Runner, image: str) -> dict:
    stdout = run(runner, ["docker", "image", "inspect", "--format", "{{json .Config}}", image], what=f"inspect of {image}")
    try:
        config = json.loads(stdout)
    except ValueError:
        die(f"docker image inspect {image} did not answer JSON -- build it first")
    if not isinstance(config, dict):
        die(f"docker image inspect {image} did not answer an image config")
    return config


def _start(runner: Runner, *, image: str, stub: Stub, work: str) -> str:
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
        handle.write(stub.credentials(host_gateway()))
    os.chmod(credentials, 0o600)
    run(runner, ["docker", "cp", staged, f"{container}:/var/lib/"], what="writing /var/lib/plow/credentials")
    run(runner, ["docker", "start", container], what=f"start of {container}")
    return container


def _stray_processes(runner: Runner, container: str) -> str | None:
    """What runs as something other than the agent's uid, besides PID 1; None when nothing does.

    `docker top` runs the host's `ps` against the container's processes, so
    this needs nothing installed in the image. Its pids are the host's, so
    PID 1 is found by asking docker which host pid it is. PID 1 is allowed to
    be root -- it has to be, to read a root-owned 0600 credential.
    """
    init = run(runner, ["docker", "inspect", "--format", "{{.State.Pid}}", container], what=f"inspect of {container}").strip()
    rows = _top(runner, container)
    if not rows:
        return "docker top listed no processes at all"
    strays = [f"pid {row[0]} is uid {row[1]}: {row[2] if len(row) > 2 else '?'}"
              for row in rows if row[0] != init and row[1] != AGENT_UID]
    if strays:
        return "; ".join(strays[:3])
    if not any(row[1] == AGENT_UID for row in rows):
        return f"no process runs as uid {AGENT_UID} -- the only one is PID 1, as uid {rows[0][1]}"
    return None


def _top(runner: Runner, container: str) -> list[list[str]]:
    """`[pid, uid, args]` for each process in the container.

    `docker top` runs whatever `ps` the daemon's host has. A busybox one --
    OrbStack's, for one -- refuses `-o uid` and only has `user`, which prints
    the uid when the host has no name for it and `root` for 0. So `uid` is
    asked first and `user` second, with `root` read back as 0.
    """
    status, stdout = runner(["docker", "top", container, "-o", "pid,uid,args"], {})
    if status != 0:
        stdout = run(runner, ["docker", "top", container, "-o", "pid,user,args"], what=f"top of {container}")
    rows = [row.split(None, 2) for row in stdout.splitlines()[1:] if row.strip()]
    return [[row[0], "0" if row[1] == "root" else row[1], *row[2:]] for row in rows if len(row) >= 2]


def _state(runner: Runner, container: str) -> tuple[str, int]:
    """(`running`/`exited`/..., exit code) as docker has them now."""
    fields = run(runner, ["docker", "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", container],
                 what=f"inspect of {container}").split()
    try:
        return fields[0], int(fields[1])
    except (ValueError, IndexError):
        die(f"docker inspect {container} did not answer a state and an exit code")


def _stop(runner: Runner, container: str, deadline_s: float) -> str | None:
    """SIGTERM the container and say what was wrong with how it went; None when it exited cleanly in time.

    `docker kill -s TERM` rather than `docker stop`: stop escalates to SIGKILL
    itself and exits 0 either way. Here the check owns the deadline.
    """
    status, code = _state(runner, container)
    if status != "running":
        return f"it had already stopped before SIGTERM was sent ({status}, exit {code})"
    sent, _ = runner(["docker", "kill", "--signal", "TERM", container], {})
    if sent != 0:
        return f"`docker kill --signal TERM` failed, exit {sent}"
    until = time.monotonic() + deadline_s
    while status == "running" and time.monotonic() < until:
        time.sleep(0.1)
        status, code = _state(runner, container)
    if status == "running":
        return f"it was still running {deadline_s:g}s after SIGTERM"
    # 143 is 128 + SIGTERM: a process that takes the default action on the
    # signal has exited because of it, which is what was asked.
    return None if code in (0, 143) else f"it exited {code}"
