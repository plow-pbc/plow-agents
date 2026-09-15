#!/usr/bin/env python3
"""A Plow cloud agent, whole, in one file.

It keeps the contract and follows its advice, and does nothing else:

  1. reads PLOW_API_BASE, and PLOW_AGENT_TOKEN if it is set, from its environment;
  2. calls GET {PLOW_API_BASE}/v1/agents/cloud/me for its line and its chats;
  3. opens the chat WebSocket and listens;
  4. answers each inbound message by POSTing one back, and exits on SIGTERM.

On exe.dev the token is not set: PLOW_API_BASE is a proxy that adds it to every
request, so it never reaches the VM. It is set for local runs, where there is
no proxy, and then it goes on every request as a bearer.

Replace `compose_reply` with your agent. Everything above it is the contract
and wants no edits; everything below it is yours.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from functools import partial

import websockets

# The identity call is a dependency of coming up, not a nicety: an agent that
# starts without it is guessing which line it is on. Retry briefly, fail closed.
IDENTITY_ATTEMPTS = 10
IDENTITY_BACKOFF_S = 2
RECONNECT_BACKOFF_S = 5

log = logging.getLogger("agent")


# --- your agent -------------------------------------------------------------


def compose_reply(body: str, sender: dict, chat: dict) -> str | None:
    """What to say back, or None to stay quiet. This is the part you replace."""
    who = sender.get("display_name") or "there"
    return f"Hi {who} — I got: {body.strip()[:200]}"


# --- the contract -----------------------------------------------------------


def read_environment(environ: dict[str, str] = os.environ) -> tuple[str, str | None]:
    """(PLOW_API_BASE, PLOW_AGENT_TOKEN or None). Read at run time, never baked in."""
    base = (environ.get("PLOW_API_BASE") or "").rstrip("/")
    if not base:
        raise SystemExit("PLOW_API_BASE is not set -- Plow sets it; so must a local run")
    return base, environ.get("PLOW_AGENT_TOKEN") or None


def call(method: str, url: str, token: str | None, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"null")


async def request(method: str, url: str, token: str | None, body: dict | None = None) -> dict:
    """`call`, awaited without holding up shutdown.

    On a daemon thread rather than `asyncio.to_thread`: the executor behind
    that is joined at exit, so a request in flight when SIGTERM arrives would
    keep the process alive for up to its whole timeout. A daemon thread is
    abandoned instead, and its result with it.
    """
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def settle(outcome: Callable[[], None]) -> None:
        if not done.done():
            outcome()

    def work() -> None:
        try:
            result = call(method, url, token, body)
        except Exception as error:  # noqa: BLE001 -- handed to the awaiting task, which decides
            outcome = partial(settle, partial(done.set_exception, error))
        else:
            outcome = partial(settle, partial(done.set_result, result))
        try:
            loop.call_soon_threadsafe(outcome)
        except RuntimeError:
            pass  # the loop is gone: the agent stopped while this was in flight

    threading.Thread(target=work, daemon=True).start()
    return await done


def transient(error: Exception) -> bool:
    """A transport failure worth retrying, as opposed to an answer.

    An HTTP 4xx is Plow answering -- a revoked token, a deleted chat -- and
    asking again gets the same answer. Retrying those would hide the one thing
    the log needs to say, so they raise.
    """
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500
    return isinstance(error, (OSError, websockets.exceptions.ConnectionClosed))


async def identify(base: str, token: str | None) -> dict:
    """Who am I, which line, which chats. Asked at boot, every boot.

    A move changes the answer without changing anything on the VM, so this is
    the only way to learn you are somewhere else.
    """
    for attempt in range(IDENTITY_ATTEMPTS):
        try:
            return await request("GET", f"{base}/v1/agents/cloud/me", token)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise SystemExit("this token names no assistant -- it was deleted, or it is not an agent's") from error
            if not transient(error):
                raise SystemExit(f"Plow refused the identity call: HTTP {error.code}") from error
            log.warning("identity call failed: HTTP %s", error.code)
        except OSError as error:
            log.warning("identity call failed: %s", error)
        if attempt < IDENTITY_ATTEMPTS - 1:
            await asyncio.sleep(IDENTITY_BACKOFF_S)
    raise SystemExit("could not reach Plow to identify -- refusing to start without a line")


async def listen(base: str, token: str | None, chats: dict[str, dict]) -> None:
    """Mint a ticket, open the socket, answer what arrives. Reconnect on transport failures only."""
    while True:
        try:
            ticket = (await request("POST", f"{base}/v1/ws/ticket", token, {}))["ticket"]
            url = f"{base.replace('http', 'ws', 1)}/v1/ws?ticket={ticket}"
            # A short close timeout: on SIGTERM the close handshake is the last
            # thing the agent does, and an unreachable Plow must not stretch it
            # past the VM's grace period.
            async with websockets.connect(url, close_timeout=2) as socket:
                log.info("connected")
                async for raw in socket:
                    await handle(json.loads(raw), base, token, chats)
        except Exception as error:
            if not transient(error):
                raise
            log.warning("socket closed: %s", type(error).__name__)
        await asyncio.sleep(RECONNECT_BACKOFF_S)


async def handle(frame: dict, base: str, token: str | None, chats: dict[str, dict]) -> None:
    """One `ChatEvent`. Only `message_received` asks for anything."""
    if frame.get("event_type") != "message_received":
        return
    chat_uid = frame["chat_id"]
    message = frame["data"]["message"]
    sender = message["sender"]
    # An outbound message is the echo of our own send, and an agent sender that
    # is not a peer is us. Both would have this agent answering itself.
    if message["direction"] != "inbound":
        return
    if sender["type"] == "agent" and sender["relationship"] != "peer":
        return
    reply = compose_reply(message["body"], sender, chats.get(chat_uid, {}))
    if reply:
        await request("POST", f"{base}/v1/chats/{chat_uid}/messages", token, {"body": reply})
        log.info("replied in %s", chat_uid)


async def run(base: str, token: str | None) -> None:
    """Be the agent until SIGTERM.

    The signal handlers go in before the first request, so there is no window
    in which a stop has to wait out a network call: whatever the agent is
    doing when SIGTERM lands -- identifying, backing off, mid-request -- is
    cancelled, and the process exits.
    """
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for received in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(received, stopping.set)

    async def agent() -> None:
        identity = await identify(base, token)
        chats = {chat["uid"]: chat for chat in identity.get("chats") or []}
        log.info("line %s, %d chat(s)", (identity.get("line") or {}).get("uid"), len(chats))
        await listen(base, token, chats)

    working = asyncio.create_task(agent())
    stopped = asyncio.create_task(stopping.wait())
    await asyncio.wait({working, stopped}, return_when=asyncio.FIRST_COMPLETED)
    if working.done():
        # The agent gave up on its own -- raise why, so the exit says it.
        stopped.cancel()
        working.result()
    working.cancel()
    await asyncio.gather(working, return_exceptions=True)
    log.info("stopping")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    base, token = read_environment()
    # AGENT_ID is set only for a listing deploy, so it is never required.
    listing = os.environ.get("AGENT_ID")
    log.info("starting%s", f", listing {listing}" if listing else "")
    # SIGTERM is how the VM is stopped; exiting on it is the whole of the shutdown advice.
    asyncio.run(run(base, token))


if __name__ == "__main__":
    main()
