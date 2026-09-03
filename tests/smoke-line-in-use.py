#!/usr/bin/env python3
"""`lines`, `mint`, and `revoke` against a stub API.

Standard library only, no network, no Plow account: drive the real CLI against
a local stub with `--api-base`.

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
    {"id": 11, "key_prefix": "cloud111", "is_active": True, "agent_id": "agt_7f3", "assistant_line": line(CLOUD, "Cloud")},
    # A self-hosted one: the same shape, no agent behind it.
    {"id": 22, "key_prefix": "local222", "is_active": True, "agent_id": None, "assistant_line": line(LOCAL, "Local")},
    # Revoked, on the line that must still read `free`.
    {"id": 33, "key_prefix": "free3333", "is_active": False, "agent_id": None, "assistant_line": line(FREE, "Free")},
    # This tool's own account key: account-wide, so it resolves to no line and
    # must not make every line look taken.
    {"id": 44, "key_prefix": None, "is_active": True, "agent_id": None, "assistant_line": None},
]


class Stub(BaseHTTPRequestHandler):
    chats: dict = CHATS
    posts: list[str] = []
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
            return self._send(200, Stub.chats)
        if self.path == "/v1/api-keys":
            return self._send(200, KEYS)
        self._send(404, {"detail": self.path})

    def do_POST(self) -> None:  # noqa: N802
        Stub.posts.append(self.path)
        if self.path == "/v1/relay/agents":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Stub.minted.append(body)
            return self._send(200, {"id": 99, "key_prefix": "minted99", "token": "plow_minted99_token", "scopes": ["chats:write", "relay:call"]})
        self._send(404, {"detail": self.path})

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/api-keys/"):
            Stub.revoked.append(self.path.rsplit("/", 1)[1])
            return self._send(200, {"status": "revoked", "id": self.path.rsplit("/", 1)[1]})
        self._send(404, {"detail": self.path})

    def log_message(self, *_: object) -> None:
        pass


def run(*argv: str, cwd: str, base: str, token: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, CLI, "--api-base", base, "--token-file", token, *argv], cwd=cwd, env=env, capture_output=True, text=True)


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
        check("lines labels its columns", listed.stdout.splitlines()[0], "LINE\tNAME\tNUMBER\tSTATUS")
        rows = {row.split("\t")[0]: row.split("\t")[3] for row in listed.stdout.splitlines()}
        check("a line nobody answers on reads free", rows.get(FREE), "free")
        check("a cloud agent's line names its agent", rows.get(CLOUD), "cloud agt_7f3")
        check("a self-hosted agent's line names its key", rows.get(LOCAL), "local 22")

        os.mkdir(os.path.join(work, "plow-credentials"))
        directory = run("mint", FREE, cwd=work, base=base, token=token)
        check("mint refuses a credential directory", directory.returncode, 1)
        check("and prints the recovery command", "docker compose down -v && rmdir plow-credentials" in directory.stderr, True)
        check("and sends no POST", Stub.posts, [])
        os.rmdir(os.path.join(work, "plow-credentials"))

        refused = run("mint", CLOUD, cwd=work, base=base, token=token)
        check("mint refuses an occupied line", refused.returncode, 1)
        check("and names who holds it", "cloud agt_7f3" in refused.stderr, True)
        check("and writes nothing", os.path.exists(os.path.join(work, "plow-credentials")), False)
        check("and mints nothing", Stub.minted, [])

        free = run("mint", FREE, cwd=work, base=base, token=token)
        check("a free line mints", free.returncode, 0)
        check("and writes the credential", os.path.exists(os.path.join(work, "plow-credentials")), True)
        check("mint does not prescribe a Compose command", "docker compose" in free.stderr, False)

        # Re-minting the line this directory's own credential already holds is
        # rotation, not a second agent -- key 22 is `local 22` on that line and
        # must not refuse itself.
        with open(os.path.join(work, "plow-credentials"), "w") as handle:
            handle.write("PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=plow_local222_token\n")
        rotated = run("mint", LOCAL, cwd=work, base=base, token=token)
        check("rotating over this file's own key is not a conflict", rotated.returncode, 0)
        check("and the key it replaced was revoked", "22" in Stub.revoked, True)

        before = list(Stub.revoked)
        cloud = run("revoke", CLOUD, cwd=work, base=base, token=token)
        check("revoke refuses a cloud-held line", cloud.returncode != 0 and "delete that agent in Plow" in cloud.stderr and Stub.revoked == before, True)

        KEYS.append({"id": 23, "is_active": True, "agent_id": None, "assistant_line": line(LOCAL, "Local")})
        ambiguous = run("revoke", LOCAL, cwd=work, base=base, token=token)
        KEYS.pop()
        check("revoke refuses ambiguous local holders", ambiguous.returncode != 0 and "2 active holders" in ambiguous.stderr and Stub.revoked == before, True)

        with open(os.path.join(work, "plow-credentials"), "w") as handle:
            handle.write("PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=plow_cloud111_token\n")
        recovered = run("revoke", LOCAL, cwd=work, base=base, token=token)
        check("revoke can recover the local key holding a line", recovered.returncode, 0)
        check("and revoked that local key", Stub.revoked[-1], "22")
        check("and preserves a file naming a different key", os.path.exists(os.path.join(work, "plow-credentials")) and "names key 11" in recovered.stderr, True)

        with open(os.path.join(work, "plow-credentials"), "w") as handle:
            handle.write("PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=plow_local222_token\n")
        matching = run("revoke", LOCAL, cwd=work, base=base, token=token)
        check("line revoke removes a file naming the revoked key", matching.returncode == 0 and not os.path.exists(os.path.join(work, "plow-credentials")), True)

        Stub.chats = {"data": []}
        empty = run("lines", cwd=work, base=base, token=token)
        check("an account with no line gets the activation command", "login --new-line" in empty.stderr, True)

        credential = os.path.join(work, "plow-credentials")
        with open(credential, "w") as handle:
            handle.write("PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=plow_local222_token\n")
        fake_bin = os.path.join(work, "bin")
        os.mkdir(fake_bin)
        docker_called = os.path.join(work, "docker-called")
        docker = os.path.join(fake_bin, "docker")
        with open(docker, "w") as handle:
            handle.write(f"#!/bin/sh\n: > {docker_called!r}\nexit 99\n")
        os.chmod(docker, 0o755)
        revoked = run("revoke", cwd=work, base=base, token=token, env=dict(os.environ, PATH=fake_bin))
        check("revoke exits 0", revoked.returncode, 0)
        check("the matching key was revoked", Stub.revoked[-1], "22")
        check("the credential file was removed", os.path.exists(credential), False)
        check("docker was never invoked", os.path.exists(docker_called), False)

        with open(credential, "w") as handle:
            handle.write("PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=plow_nomatch0_token\n")
        before = list(Stub.revoked)
        unmatched = run("revoke", cwd=work, base=base, token=token)
        check("a credential matching no key is refused", unmatched.returncode != 0 and "no key matches" in unmatched.stderr, True)
        check("the unmatched credential remains", os.path.exists(credential), True)
        check("and no key was revoked", Stub.revoked, before)

    server.shutdown()
    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
