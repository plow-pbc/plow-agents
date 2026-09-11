"""A Plow API just large enough for `image check` to boot one agent against.

Not a Plow simulator. It answers four things -- the identity call, a WebSocket
ticket, the socket itself, and one message POST -- and records what the agent
did, in the order it did it. Everything else is a 404, which is the same
answer an agent reaching for a route this check does not cover would get.

The WebSocket half is hand-rolled because it is four frames: the handshake, a
`connected` frame, one `message_received` frame, and whatever the agent sends
back before it is asked to stop.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# RFC 6455's constant, concatenated with the client's key to prove the server
# read the handshake rather than merely accepted the socket.
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

LINE_UID = "ln_check"
CHAT_UID = "cht_check"
MEMBER_UID = "usr_check"
PROMPT = "plow-agents image check: reply with anything."


class Recorder:
    """What the agent did, in order, and the one thing it said."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.bad_auth: list[str] = []
        self.reply: str | None = None
        self.replied = threading.Event()
        self._lock = threading.Lock()

    def saw(self, event: str) -> None:
        with self._lock:
            if event not in self.events:
                self.events.append(event)

    def said(self, body: str) -> None:
        self.reply = body
        self.replied.set()


def _accept_key(client_key: str) -> str:
    return base64.b64encode(hashlib.sha1(client_key.encode() + WS_GUID).digest()).decode()


def _text_frame(payload: dict) -> bytes:
    """One unmasked server-to-client text frame. The check's frames are small."""
    body = json.dumps(payload).encode()
    if len(body) < 126:
        header = struct.pack("!BB", 0x81, len(body))
    else:
        header = struct.pack("!BBH", 0x81, 126, len(body))
    return header + body


def _read_frame(stream) -> tuple[int, bytes] | None:
    """One masked client-to-server frame, or None at end of stream."""
    head = stream.read(2)
    if len(head) < 2:
        return None
    opcode = head[0] & 0x0F
    masked, length = head[1] & 0x80, head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", stream.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", stream.read(8))[0]
    key = stream.read(4) if masked else b"\0\0\0\0"
    body = stream.read(length)
    if masked:
        body = bytes(byte ^ key[index % 4] for index, byte in enumerate(body))
    return opcode, body


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def stub(self) -> Stub:
        return self.server.stub  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {self.stub.token}":
            return True
        self.stub.seen.bad_auth.append(f"{self.command} {self.path}")
        self._json(401, {"detail": "not this agent's token"})
        return False

    def _json(self, status: int, payload: object) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        if self.path.startswith("/v1/ws?") or self.path == "/v1/ws":
            return self._websocket()
        if self.path == "/v1/agents/cloud/me":
            # Recorded before the token is judged: an agent that presented the
            # wrong one did make the call, and saying "no identity call
            # arrived" would send its author looking in the wrong place.
            self.stub.seen.saw("identity")
            if not self._authorized():
                return
            return self._json(200, self.stub.identity())
        self._json(404, {"detail": self.path})

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/v1/ws/ticket":
            self.stub.seen.saw("ticket")
            if not self._authorized():
                return
            return self._json(200, {"ticket": self.stub.ticket})
        if self.path == f"/v1/chats/{CHAT_UID}/messages":
            self.stub.seen.saw("reply")
            if not self._authorized():
                return
            self.stub.seen.said((json.loads(body or b"{}").get("body") or "").strip())
            return self._json(201, {"uid": "msg_reply"})
        self._json(404, {"detail": self.path})

    def _websocket(self) -> None:
        """Upgrade, say `connected`, deliver one message, then listen until closed."""
        key = self.headers.get("Sec-WebSocket-Key")
        _, _, query = self.path.partition("?")
        ticket = dict(part.split("=", 1) for part in query.split("&") if "=" in part).get("ticket")
        if not key or ticket != self.stub.ticket:
            self.stub.seen.bad_auth.append(f"WS {self.path}")
            return self._json(401, {"detail": "no ticket, or not this run's"})
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + _accept_key(key).encode() + b"\r\n\r\n"
        )
        self.wfile.flush()
        self.stub.seen.saw("websocket")
        self.wfile.write(_text_frame({"type": "connected"}))
        self.wfile.write(_text_frame(self.stub.message_frame()))
        self.wfile.flush()
        self.stub.seen.saw("delivered")
        while True:
            frame = _read_frame(self.rfile)
            if frame is None or frame[0] == 0x8:
                return
            if frame[0] == 0x9:  # ping
                self.wfile.write(b"\x8a" + bytes([len(frame[1])]) + frame[1])
                self.wfile.flush()

    def log_message(self, *_: object) -> None:
        pass


class Stub:
    """The stub, its synthetic credential, and what the agent did with it."""

    def __init__(self, *, host: str = "0.0.0.0") -> None:  # noqa: S104 -- the container has to reach it
        self.token = "plow_check_" + os.urandom(16).hex()
        self.ticket = "tkt_" + os.urandom(16).hex()
        self.seen = Recorder()
        self._server = ThreadingHTTPServer((host, 0), _Handler)
        self._server.stub = self  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]

    def __enter__(self) -> Stub:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def base_for(self, host: str) -> str:
        """The `PLOW_API_BASE` to write into the credential, from where it is read."""
        return f"http://{host}:{self.port}"

    def credentials(self, host: str, agent_id: str) -> str:
        """The three lines Plow writes, and nothing else."""
        return f"AGENT_ID={agent_id}\nPLOW_API_BASE={self.base_for(host)}\nPLOW_AGENT_TOKEN={self.token}\n"

    def identity(self) -> dict:
        """`GET /v1/agents/cloud/me`: one line, one chat, no Mac, a signup phrase."""
        return {
            "line": {"uid": LINE_UID, "display_name": "image check", "provider_key": "+15555550100", "agent_uid": "agt_check"},
            "chats": [{
                "uid": CHAT_UID, "type": "dm", "status": "active",
                "participants": [
                    {"type": "member", "uid": MEMBER_UID, "role": "owner", "display_name": "Owner"},
                    {"type": "agent", "relationship": "self", "line": {"uid": LINE_UID}},
                ],
            }],
            "mcp_url": None,
            "signup": {"name": "image check", "phrase": "text this to start"},
        }

    def message_frame(self) -> dict:
        """One inbound message, shaped as the socket delivers it."""
        return {
            "type": "event", "event_type": "message_received", "event_id": "evt_check", "chat_id": CHAT_UID,
            "data": {"message": {
                "uid": "msg_check", "chat_id": CHAT_UID, "direction": "inbound", "body": PROMPT,
                "attachments": [], "sender": {"type": "member", "uid": MEMBER_UID, "display_name": "Owner"},
            }},
        }


def host_gateway() -> str:
    """The name a container reaches this machine by, and the flag that makes it resolve."""
    return "host.docker.internal"


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
