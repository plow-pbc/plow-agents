#!/usr/bin/env python3
"""`login`, `profile` and the account token's transport against a stub API.

Standard library only, no network, no Plow account: drive the real CLI against
a local stub with `--api-base`.

    python3 tests/smoke-account.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "plow-agents")

PHOTO_URL = "https://api.example.com/v1/profile-photos/2b0f9c1e-0000-4000-8000-000000000001"


class Stub(BaseHTTPRequestHandler):
    profile_updates: list[dict] = []
    photo_uploads: list[dict[str, tuple[str, bytes]]] = []
    requests: list[str] = []
    profile_get_status = 200

    def _send(self, status: int, payload: object) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        Stub.requests.append(f"GET {self.path}")
        if self.path == "/v1/auth/owner-uid":
            return self._send(200, {"owner_uid": "12345678-1234-4678-9234-567812345678"})
        if self.path == "/v1/auth/profile":
            return self._send(Stub.profile_get_status, {"display_name": "Ada", "photo_url": "https://example.com/ada.jpg"})
        self._send(404, {"detail": self.path})

    def do_PATCH(self) -> None:  # noqa: N802
        Stub.requests.append(f"PATCH {self.path}")
        if self.path == "/v1/auth/profile":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Stub.profile_updates.append(body)
            return self._send(200, body)
        self._send(404, {"detail": self.path})

    def do_POST(self) -> None:  # noqa: N802
        Stub.requests.append(f"POST {self.path}")
        if self.path == "/v1/auth/activate":
            return self._send(200, {"display_code": "test", "activation_secret": "synthetic", "send_to": "+15555555555"})
        if self.path == "/v1/auth/activate/redeem":
            return self._send(200, {"status": "verified", "token": "synthetic_account_token"})
        if self.path == "/v1/auth/profile/photo":
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            # The part's own bytes, recovered the way a server does: split on
            # the boundary the Content-Type declared, then past the blank line
            # that ends the part's headers.
            boundary = self.headers["Content-Type"].split("boundary=", 1)[1].encode()
            parts = {}
            for part in raw.split(b"--" + boundary)[1:-1]:
                headers, _, content = part.partition(b"\r\n\r\n")
                field = headers.decode().partition('name="')[2].partition('"')[0]
                parts[field] = (headers.decode(), content.rpartition(b"\r\n")[0])
            Stub.photo_uploads.append(parts)
            name = parts["display_name"][1].decode() if "display_name" in parts else None
            return self._send(200, {"display_name": name, "photo_url": PHOTO_URL})
        self._send(404, {"detail": self.path})

    def log_message(self, *_: object) -> None:
        pass


class Proxy(BaseHTTPRequestHandler):
    received: list[tuple[str, str | None, bytes]] = []

    def do_GET(self) -> None:  # noqa: N802
        Proxy.received.append((self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        self.send_response(502)
        self.end_headers()

    do_POST = do_GET

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

        Stub.requests.clear()
        profile = run("profile", "--name", "Ada", "--photo", "https://example.com/ada.jpg", cwd=work, base=base, token=token)
        check("profile update exits 0", profile.returncode, 0)
        check("profile update sends name and photo", Stub.profile_updates[-1], {"display_name": "Ada", "photo_url": "https://example.com/ada.jpg"})

        check("a photo already hosted is one PATCH and no upload", Stub.requests, ["PATCH /v1/auth/profile"])

        # Schemes are case-insensitive. A prefix test that was not read
        # HTTPS://... as a filename and went looking for it on disk.
        for written in ("HTTPS://example.com/ada.jpg", "Https://example.com/ada.jpg", "HTTP://example.com/ada.jpg"):
            Stub.requests.clear()
            cased = run("profile", "--name", "Ada", "--photo", written, cwd=work, base=base, token=token)
            check(f"{written.split(':')[0]} is a URL, not a filename", Stub.requests, ["PATCH /v1/auth/profile"])
            check(f"and {written.split(':')[0]} is sent as given", cased.returncode == 0 and Stub.profile_updates[-1]["photo_url"] == written, True)

        # A local file is uploaded, and that route stores the bytes and sets
        # photo_url in one transaction -- so the upload is the whole write and
        # nothing may follow it. A PATCH afterwards that failed would report an
        # error for a photo already stored and already public.
        photo = os.path.join(work, "ada photo.png")
        with open(photo, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\nada")
        Stub.requests.clear()
        uploaded = run("profile", "--photo", photo, cwd=work, base=base, token=token)
        check("a local file exits 0", uploaded.returncode, 0)
        check("and is exactly one request", Stub.requests, ["POST /v1/auth/profile/photo"])
        check("with the file's own bytes", Stub.photo_uploads[-1]["file"][1], b"\x89PNG\r\n\x1a\nada")
        check("under a filename with nothing that could break the header", 'filename="ada_photo.png"' in Stub.photo_uploads[-1]["file"][0], True)
        check("and prints the profile the route answered with", json.loads(uploaded.stdout)["photo_url"], PHOTO_URL)

        # A name alongside a file rides in the same request, because the route
        # writes both as it stores the bytes. Two calls meant a failure at the
        # second left the photo public beside the name it was sent to replace.
        Stub.requests.clear()
        both = run("profile", "--name", "Ada", "--photo", photo, cwd=work, base=base, token=token)
        check("a name beside a file is still one request", Stub.requests, ["POST /v1/auth/profile/photo"])
        check("carrying both parts", Stub.photo_uploads[-1]["display_name"][1], b"Ada")
        check("and it exits 0", both.returncode, 0)
        check("and prints the profile with both halves set", json.loads(both.stdout), {"display_name": "Ada", "photo_url": PHOTO_URL})

        # One harness for every way a file can be unusable: each is refused
        # before the first request, so the account is left exactly as it was.
        huge = b"\x89PNG\r\n\x1a\n" + b"\0" * (5 * 1024 * 1024)
        for label, filename, blob, said in (
            ("a path that is not there", "nope.png", None, "cannot read"),
            ("a file that is not an image", "notes.txt", b"dear diary", "not a PNG, JPEG, GIF, or WebP"),
            ("a file over the cap", "huge.png", huge, "larger than 5 MB"),
        ):
            unusable = os.path.join(work, filename)
            if blob is not None:
                with open(unusable, "wb") as handle:
                    handle.write(blob)
            Stub.requests.clear()
            refused = run("profile", "--name", "Ada", "--photo", unusable, cwd=work, base=base, token=token)
            check(f"{label} is refused", refused.returncode != 0 and said in refused.stderr, True)
            check(f"and {label} makes zero requests", Stub.requests, [])

        Stub.requests.clear()
        named = run("profile", "--name", "Ada", cwd=work, base=base, token=token)
        check("a name on its own is one PATCH", Stub.requests, ["PATCH /v1/auth/profile"])
        check("and exits 0", named.returncode, 0)
        neither = run("profile", cwd=work, base=base, token=token)
        check("and asking for nothing is refused", neither.returncode != 0 and "needs --name, --photo, or --show" in neither.stderr, True)

        shown = run("profile", "--show", cwd=work, base=base, token=token)
        check("profile show exits 0", shown.returncode, 0)
        check("profile show includes account uid", json.loads(shown.stdout)["uid"], "12345678-1234-4678-9234-567812345678")
        Stub.profile_get_status = 500
        failed_show = run("profile", "--show", cwd=work, base=base, token=token)
        check("profile show fails on a failed GET", failed_show.returncode != 0, True)
        Stub.profile_get_status = 200

        Stub.requests.clear()
        logged_in = run("login", cwd=work, base=base, token=token)
        check("login only activates and redeems", logged_in.returncode == 0 and Stub.requests == ["POST /v1/auth/activate", "POST /v1/auth/activate/redeem"], True)
        for verb in ("lines", "agents", "mint", "deploy", "revoke", "rotate"):
            gone = run(verb, cwd=work, base=base, token=token)
            check(f"{verb} is no longer a command", gone.returncode == 2 and "invalid choice" in gone.stderr, True)
        Stub.requests.clear()
        refused = run("profile", "--name", "Ada", cwd=work, base=base+'"', token=token)
        check("invalid host API base is refused without requests", refused.returncode != 0 and not Stub.requests, True)
        cli_main = runpy.run_path(CLI)["main"]
        for root, permitted in (
            ("https://api.example.com", True),
            ("http://localhost:8000", True),
            ("http://127.0.0.1:8000", True),
            ("http://[::1]:8000", True),
            ("http://api.orb.local:8000", True),
            ("http://api.example.com", False),
            ("http://localhost.example.com", False),
            ("http://api.orb.local.example.com", False),
            ("http://localhost@api.example.com", False),
        ):
            response = io.BytesIO(b'{}')
            response.status = 200
            argv = [CLI, "--api-base", root, "--token-file", token, "profile", "--name", "Ada"]
            with patch.object(sys, "argv", argv), patch("urllib.request.OpenerDirector.open", return_value=response) as transport:
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    try:
                        code = cli_main()
                    except SystemExit as error:
                        code = error.code
            check(f"account token transport policy for {root}", (code == 0, transport.called), (permitted, permitted))
        proxy = HTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        cli_request = runpy.run_path(CLI)["request"]
        resolve = socket.getaddrinfo
        proxy_env = {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url, "no_proxy": "", "NO_PROXY": ""}
        try:
            with patch.dict(os.environ, proxy_env), patch("urllib.request._opener", None), patch("urllib.request.proxy_bypass", return_value=False):
                for host in ("localhost", "127.0.0.1", "[::1]", "api.orb.local"):
                    dev_root = f"http://{host}:{server.server_address[1]}"
                    Stub.requests.clear()
                    with patch("socket.getaddrinfo", side_effect=lambda host, port, *args, **kwargs: resolve("127.0.0.1", port, *args, **kwargs)):
                        cli_request("GET", dev_root + "/v1/auth/profile", token="synthetic_account_token")
                        cli_request("POST", dev_root + "/v1/auth/activate/redeem", body={"activation_secret": "synthetic_activation_secret"})
                    check(f"{host} sends both secrets directly to the origin", Stub.requests, ["GET /v1/auth/profile", "POST /v1/auth/activate/redeem"])
                check("configured proxy receives no request or secret", Proxy.received, [])
        finally:
            proxy.shutdown()
            proxy.server_close()

    server.shutdown()
    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
