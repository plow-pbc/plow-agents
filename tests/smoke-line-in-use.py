#!/usr/bin/env python3
"""`lines` and `mint` against a stub API holding all three states.

Standard library only, no network, no Plow account: a stub that answers the two
reads `lines` makes and the mint `mint` makes, and the real CLI driven against
it with `--api-base`. What is under test is the join -- which line each key
claims, and whether a line already answering refuses a second agent.

    python3 tests/smoke-line-in-use.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "plow-agents")

FREE, CLOUD, LOCAL = "ln_free", "ln_cloud", "ln_local"


def line(uid: str, name: str) -> dict:
    return {"uid": uid, "display_name": name, "provider_key": f"+1555{uid[-4:]}"}


CHATS = {"data": [{"participants": [{"type": "agent", "line": line(uid, name)}]} for uid, name in ((FREE, "Free"), (CLOUD, "Cloud"), (LOCAL, "Local"))]}

KEYS = [
    # The cloud agent's credential: a line, and an agent_id naming its agent.
    {"id": 11, "is_active": True, "agent_id": "agt_7f3", "assistant_line": line(CLOUD, "Cloud")},
    # A self-hosted one: the same shape, no agent behind it.
    {"id": 22, "is_active": True, "agent_id": None, "assistant_line": line(LOCAL, "Local")},
    # Revoked, on the line that must still read `free`.
    {"id": 33, "is_active": False, "agent_id": None, "assistant_line": line(FREE, "Free")},
    # This tool's own account key: account-wide, so it resolves to no line and
    # must not make every line look taken.
    {"id": 44, "is_active": True, "agent_id": None, "assistant_line": None},
]


class Stub(BaseHTTPRequestHandler):
    minted: list[dict] = []
    revoked: list[str] = []

    def _send(self, status: int, payload: object) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        if self.path == "/v1/chats":
            return self._send(200, CHATS)
        if self.path == "/v1/api-keys":
            return self._send(200, KEYS)
        self._send(404, {"detail": self.path})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/relay/agents":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Stub.minted.append(body)
            return self._send(200, {"id": 99, "token": "tok_stub", "scopes": ["chats:write", "relay:call"]})
        self._send(404, {"detail": self.path})

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/api-keys/"):
            Stub.revoked.append(self.path.rsplit("/", 1)[1])
            return self._send(200, {"status": "revoked", "id": self.path.rsplit("/", 1)[1]})
        self._send(404, {"detail": self.path})

    def log_message(self, *_: object) -> None:
        pass


def run(*argv: str, cwd: str, base: str, token: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, CLI, "--api-base", base, "--token-file", token, *argv], cwd=cwd, capture_output=True, text=True)


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    failures = []

    def check(what: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{what}\n  want: {want!r}\n  got:  {got!r}")
        print(f"{'ok  ' if got == want else 'FAIL'} {what}")

    with tempfile.TemporaryDirectory() as work:
        token = os.path.join(work, "token")
        with open(token, "w") as handle:
            handle.write("acct_stub\n")

        listed = run("lines", cwd=work, base=base, token=token)
        check("lines exits 0", listed.returncode, 0)
        rows = {row.split("\t")[0]: row.split("\t")[3] for row in listed.stdout.splitlines()}
        check("a line nobody answers on reads free", rows.get(FREE), "free")
        check("a cloud agent's line names its agent", rows.get(CLOUD), "cloud agt_7f3")
        check("a self-hosted agent's line names its key", rows.get(LOCAL), "local 22")

        refused = run("mint", CLOUD, cwd=work, base=base, token=token)
        check("mint refuses an occupied line", refused.returncode, 1)
        check("and names who holds it", "cloud agt_7f3" in refused.stderr, True)
        check("and writes nothing", os.path.exists(os.path.join(work, "plow-credentials")), False)
        check("and mints nothing", Stub.minted, [])

        forced = run("mint", CLOUD, "--force", cwd=work, base=base, token=token)
        check("--force mints anyway", forced.returncode, 0)
        check("and writes the credential", os.path.exists(os.path.join(work, "plow-credentials")), True)

        free = run("mint", FREE, cwd=work, base=base, token=token)
        check("a free line mints without --force", free.returncode, 0)

        # Re-minting the line this directory's own credential already holds is
        # rotation, not a second agent -- key 22 is `local 22` on that line and
        # must not refuse itself.
        with open(os.path.join(work, "plow-credentials"), "w") as handle:
            handle.write("# plow-agents-key-id: 22\nPLOW_API_BASE=x\nPLOW_AGENT_TOKEN=y\n")
        rotated = run("mint", LOCAL, cwd=work, base=base, token=token)
        check("rotating over this file's own key is not a conflict", rotated.returncode, 0)
        check("and the key it replaced was revoked", "22" in Stub.revoked, True)

    server.shutdown()
    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
