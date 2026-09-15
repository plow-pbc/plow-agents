"""A Plow API just large enough for `image check` to boot one agent against.

Not a Plow simulator. It answers four things -- the identity call, a WebSocket
ticket, the socket itself, and one message POST -- and records what the agent
did, in the order it did it. Everything else is a 404, which is the same
answer an agent reaching for a route this check does not cover would get.

The WebSocket half is hand-rolled because it is four frames: the handshake, a
`connected` frame, one `message_received` frame, and whatever the agent sends
back before it is asked to stop.

Neither the `message_received` frame nor the identity answer is written here.
They are `message_received.json` and `identity.json`, serialized by the Plow
API's own models (`tests/regenerate-fixtures.py` in this repo makes them from a
Plow checkout), so an agent written against the published schema reads them
exactly as it will read the real thing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

# RFC 6455's constant, concatenated with the client's key to prove the server
# read the handshake rather than merely accepted the socket.
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_BODY = 64 * 1024

CHAT_UID = "cht_check"
# One account-socket `ChatEvent`, as the Plow API serializes it: the envelope
# with its `data`, because an agent's ticket names no chat.
MESSAGE_RECEIVED = json.loads(resources.files(__package__).joinpath("message_received.json").read_text())
# `AgentIdentity`, as `GET /v1/agents/cloud/me` serves it.
IDENTITY = json.loads(resources.files(__package__).joinpath("identity.json").read_text())


class Recorder:
    """What the agent did, in order, and the one thing it said."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.bad_auth: list[str] = []
        self.reply: str | None = None
        self.replied = threading.Event()
        # Set by any request, to any path, carrying this run's token.
        self.token = threading.Event()
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

    def _route(self) -> bool:
        """Strip this run's unguessable prefix, or answer 404 having noted nothing.

        The stub listens on every interface and the tokenless boot shape gives
        the container no secret to present, so the prefix is the secret: only
        something handed this run's `PLOW_API_BASE` can be under it, and a LAN
        peer guessing routes never reaches the recorder.
        """
        realm = self.stub.realm
        if self.path == realm or self.path.startswith(realm + "/"):
            self.path = self.path[len(realm):] or "/"
            return True
        self._json(404, {"detail": self.path}, close=True)
        return False

    def _note_token(self) -> None:
        # No token of its own is the exe.dev shape: the integration in front of
        # the API adds the bearer, so whatever arrives here already carries the
        # agent's authority and the call itself is the evidence.
        if self.stub.token is None or self.headers.get("Authorization") == f"Bearer {self.stub.token}":
            self.stub.seen.token.set()

    def _authorized(self) -> bool:
        if self.stub.token is None or self.headers.get("Authorization") == f"Bearer {self.stub.token}":
            return True
        self.stub.seen.bad_auth.append(f"{self.command} {self.path}")
        self._json(401, {"detail": "not this agent's token"}, close=True)
        return False

    def _json(self, status: int, payload: object, *, close: bool = False) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if close:
            # A body left unread would be parsed as the next request.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        if not self._route():
            return
        self._note_token()
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
        # The stub listens on every interface, so a body is read only once the
        # realm, the route and the token say whose it is, and only up to MAX_BODY.
        if not self._route():
            return
        self._note_token()
        event = {"/v1/ws/ticket": "ticket", f"/v1/chats/{CHAT_UID}/messages": "reply"}.get(self.path)
        if event is None:
            return self._json(404, {"detail": self.path}, close=True)
        self.stub.seen.saw(event)
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"detail": f"a body over {MAX_BODY} bytes"}, close=True)
        body = self.rfile.read(length)
        if event == "ticket":
            return self._json(201, {"object": "ws_ticket", "ticket": self.stub.ticket,
                                    "expires_at": "2099-01-01T00:00:00Z"})
        self.stub.seen.said((json.loads(body or b"{}").get("body") or "").strip())
        return self._json(201, {"uid": "msg_reply"})

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
            if frame is None:
                return
            if frame[0] == 0x8:
                # Echo the close, as any server does. A client that closes
                # properly waits for this, and without it would sit out its
                # close timeout -- past the SIGTERM deadline -- on the stub's
                # account rather than its own.
                self.wfile.write(b"\x88" + bytes([len(frame[1][:2])]) + frame[1][:2])
                self.wfile.flush()
                return
            if frame[0] == 0x9:  # ping
                self.wfile.write(b"\x8a" + bytes([len(frame[1])]) + frame[1])
                self.wfile.flush()

    def log_message(self, *_: object) -> None:
        pass


class Stub:
    """The stub, its synthetic token, and what the agent did with it."""

    def __init__(self, *, host: str = "0.0.0.0", token: str | None = None) -> None:  # noqa: S104 -- the container has to reach it
        self.token = token
        # Every route this run answers hangs off here. See `_route`.
        self.realm = "/chk_" + os.urandom(16).hex()
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

    def environment(self, host: str) -> dict[str, str]:
        """What the container is given, reaching the stub by `host`.

        `PLOW_API_BASE` alone, which is what a VM gets: exe.dev's integration
        adds the bearer on the way through, so neither `PLOW_AGENT_TOKEN` nor
        `AGENT_ID` is set there. An image that requires either would fail on
        every bare-image deploy, and this is where it should find that out.

        The base carries this run's realm, so the container is the only thing
        on the network that knows where the stub answers.
        """
        environment = {"PLOW_API_BASE": f"http://{host}:{self.port}{self.realm}"}
        return environment if self.token is None else {**environment, "PLOW_AGENT_TOKEN": self.token}

    def identity(self) -> dict:
        """`GET /v1/agents/cloud/me`: one line, one chat, no Mac, a signup phrase."""
        return IDENTITY

    def message_frame(self) -> dict:
        """One inbound message, shaped as the socket delivers it."""
        return MESSAGE_RECEIVED


def host_gateway() -> str:
    """The name a container reaches this machine by, and the flag that makes it resolve."""
    return "host.docker.internal"
