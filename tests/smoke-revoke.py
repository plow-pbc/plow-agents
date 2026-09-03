#!/usr/bin/env python3
"""`revoke` retires one credential without invoking Docker.

    python3 tests/smoke-revoke.py
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


class Stub(BaseHTTPRequestHandler):
    revoked: list[str] = []

    def do_DELETE(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        Stub.revoked.append(self.path)
        raw = json.dumps({"status": "revoked"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_: object) -> None:
        pass


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
        credential = os.path.join(work, "plow-credentials")
        docker_called = os.path.join(work, "docker-called")
        with open(token, "w") as handle:
            handle.write("acct_stub\n")
        with open(credential, "w") as handle:
            handle.write("# plow-agents-key-id: 42\nPLOW_API_BASE=x\nPLOW_AGENT_TOKEN=y\n")

        fake_bin = os.path.join(work, "bin")
        os.mkdir(fake_bin)
        docker = os.path.join(fake_bin, "docker")
        with open(docker, "w") as handle:
            handle.write(f"#!/bin/sh\n: > {docker_called!r}\nexit 99\n")
        os.chmod(docker, 0o755)
        env = dict(os.environ, PATH=fake_bin)
        done = subprocess.run(
            [sys.executable, CLI, "--api-base", base, "--token-file", token, "revoke"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
        )
        check("revoke exits 0", done.returncode, 0)
        check("the named key was revoked", Stub.revoked, ["/v1/api-keys/42"])
        check("the credential file was removed", os.path.exists(credential), False)
        check("docker was never invoked", os.path.exists(docker_called), False)

        with open(credential, "w") as handle:
            handle.write("PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=y\n")
        refused = subprocess.run(
            [sys.executable, CLI, "--api-base", "http://127.0.0.1:9", "--token-file", token, "revoke"],
            cwd=work,
            capture_output=True,
            text=True,
        )
        check("a credential without one key id is refused", refused.returncode != 0, True)
        check("the untracked credential remains", os.path.exists(credential), True)
        check("the error points to the dashboard", "dashboard" in refused.stderr, True)

    server.shutdown()
    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
