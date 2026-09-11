#!/usr/bin/env python3
"""A Plow cloud agent, whole, in one file.

It does the four things the contract asks of an image and nothing else:

  1. reads /var/lib/plow/credentials as root, then drops to uid 10000;
  2. calls GET {PLOW_API_BASE}/v1/agents/cloud/me for its line and its chats;
  3. opens the chat WebSocket and listens;
  4. answers each inbound message by POSTing one back, and exits on SIGTERM.

Plow writes that credential root-owned 0600, so PID 1 starts as root -- and
stops being root three lines later, before anything touches the network. That
split is the whole reason this file has a `become_agent`.

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
import urllib.error
import urllib.request

import websockets

CREDENTIALS = os.environ.get("PLOW_CREDENTIALS", "/var/lib/plow/credentials")
# The contract's uid/gid. Everything after the credential read runs as this.
AGENT_UID = AGENT_GID = 10000
# The identity call is a dependency of coming up, not a nicety: an agent that
# starts without it is guessing which line it is on. Retry briefly, fail closed.
IDENTITY_ATTEMPTS = 10
IDENTITY_BACKOFF_S = 2

log = logging.getLogger("agent")


# --- your agent -------------------------------------------------------------


def compose_reply(body: str, sender: dict, chat: dict) -> str | None:
    """What to say back, or None to stay quiet. This is the part you replace."""
    who = sender.get("display_name") or "there"
    return f"Hi {who} — I got: {body.strip()[:200]}"


# --- the contract -----------------------------------------------------------


def read_credentials(path: str = CREDENTIALS) -> dict[str, str]:
    """The file Plow writes: root-owned 0600, three KEY=value lines.

    Read as data. Plow owns the path and the permissions; what the agent does
    with the values afterwards is the agent's business.
    """
    values = {}
    with open(path) as handle:
        for raw in handle:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    missing = [key for key in ("AGENT_ID", "PLOW_API_BASE", "PLOW_AGENT_TOKEN") if not values.get(key)]
    if missing:
        raise SystemExit(f"{path} is missing {', '.join(missing)}")
    return values


def become_agent(uid: int = AGENT_UID, gid: int = AGENT_GID) -> None:
    """Drop root, for good, the moment the credential has been read.

    Groups first, then gid, then uid: after `setuid` there is no privilege left
    to change the others with, and a drop that leaves a supplementary group
    behind has not dropped anything. Already unprivileged (a local run as
    yourself) is fine and does nothing.
    """
    if os.getuid() != 0:
        return
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.getuid() != uid or os.geteuid() != uid:
        raise SystemExit("could not drop to uid 10000 -- refusing to run as root")


def call(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"null")


def identify(base: str, token: str) -> dict:
    """Who am I, which line, which chats. Asked at boot, every boot.

    A move changes the answer without changing anything on the VM, so this is
    the only way to learn you are somewhere else.
    """
    for attempt in range(IDENTITY_ATTEMPTS):
        try:
            return call("GET", f"{base}/v1/agents/cloud/me", token)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise SystemExit("this token names no assistant -- it was deleted, or it is not an agent's") from error
            log.warning("identity call failed: HTTP %s", error.code)
        except OSError as error:
            log.warning("identity call failed: %s", error)
        if attempt < IDENTITY_ATTEMPTS - 1:
            import time
            time.sleep(IDENTITY_BACKOFF_S)
    raise SystemExit("could not reach Plow to identify -- refusing to start without a line")


async def listen(base: str, token: str, chats: dict[str, dict]) -> None:
    """Mint a ticket, open the socket, answer what arrives. Reconnect forever."""
    while True:
        try:
            ticket = call("POST", f"{base}/v1/ws/ticket", token, {})["ticket"]
            url = f"{base.replace('http', 'ws', 1)}/v1/ws?ticket={ticket}"
            async with websockets.connect(url) as socket:
                log.info("connected")
                async for raw in socket:
                    await handle(json.loads(raw), base, token, chats)
        except Exception as error:  # noqa: BLE001 -- reconnect, never die
            log.warning("socket closed: %s", type(error).__name__)
        await asyncio.sleep(5)


async def handle(frame: dict, base: str, token: str, chats: dict[str, dict]) -> None:
    if frame.get("type") == "connected" or frame.get("event_type") != "message_received":
        return
    chat_uid = frame.get("chat_id")
    message = frame.get("data", {}).get("message") or {}
    sender = message.get("sender") or {}
    # An outbound message is the echo of our own send, and an agent sender that
    # is not a peer is us. Both would have this agent answering itself.
    if message.get("direction") != "inbound" or sender.get("type") not in ("member", "agent"):
        return
    if sender.get("type") == "agent" and sender.get("relationship") != "peer":
        return
    reply = compose_reply(message.get("body") or "", sender, chats.get(chat_uid, {}))
    if reply:
        await asyncio.to_thread(call, "POST", f"{base}/v1/chats/{chat_uid}/messages", token, {"body": reply})
        log.info("replied in %s", chat_uid)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    credentials = read_credentials()
    become_agent()
    base = credentials["PLOW_API_BASE"].rstrip("/")
    token = credentials["PLOW_AGENT_TOKEN"]
    log.info("starting as %s, uid %d", credentials["AGENT_ID"], os.getuid())
    identity = identify(base, token)
    chats = {chat["uid"]: chat for chat in identity.get("chats") or []}
    log.info("line %s, %d chat(s)", (identity.get("line") or {}).get("uid"), len(chats))

    # SIGTERM is how the VM is stopped. Exiting on it is the whole of the
    # shutdown contract; a container killed after the grace period fails the check.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stopping = loop.create_future()
    for received in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(received, lambda: stopping.done() or stopping.set_result(None))
    listener = loop.create_task(listen(base, token, chats))
    loop.run_until_complete(stopping)
    listener.cancel()
    log.info("stopping")


if __name__ == "__main__":
    main()
