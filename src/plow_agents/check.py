"""Run a built image the way exe.dev would, and check it against the contract.

The contract is three lines, stated in `api/cloud-agents/README.md`: the
image's CMD is PID 1, the agent reads PLOW_API_BASE from its environment and
talks to it, and it sends PLOW_AGENT_TOKEN as a bearer when that is set. So two
things fail this check -- the image has a CMD to start, and something inside
calls the stub with the token it was given. The identity call, the WebSocket
and a reply are printed as warnings, and never fail it.

Nothing here knows about Hermes. An image passes because it satisfies the
contract, not because of what it is built from.
"""

from __future__ import annotations

import json

from .api import die, log
from .docker import Runner, run
from .stub import Stub, host_gateway

# Long enough for a cold container to start a runtime and make one call, short
# enough that a wedged image fails the check rather than the person's patience.
BOOT_TIMEOUT_S = 90
# Once the token has been used, how long the agent gets to open the socket and
# answer before the advice is judged on what it has done so far.
ADVICE_WAIT_S = 15
TOKEN_ASSERTION = "something inside calls the Plow API with PLOW_AGENT_TOKEN"


class ContractError(Exception):
    """One named assertion, and what was seen instead."""

    def __init__(self, assertion: str, saw: str) -> None:
        super().__init__(assertion)
        self.assertion, self.saw = assertion, saw


def check(runner: Runner, *, image: str, timeout: float = BOOT_TIMEOUT_S,
          advice_wait: float = ADVICE_WAIT_S) -> tuple[list[str], list[str]]:
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

    with Stub() as stub:
        container = _start(runner, image=image, stub=stub)
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


def _start(runner: Runner, *, image: str, stub: Stub) -> str:
    """Start the container with no command override and the contract's environment. Returns its id."""
    environment = [part for key, value in stub.environment(host_gateway()).items() for part in ("--env", f"{key}={value}")]
    started = run(
        runner,
        ["docker", "run", "--detach", "--add-host", f"{host_gateway()}:host-gateway", *environment, image],
        what=f"start of {image}",
    ).strip().splitlines()
    if not started or not started[-1].strip():
        die(f"docker run {image} returned no container id")
    return started[-1].strip()
