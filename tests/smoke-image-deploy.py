#!/usr/bin/env python3
"""`init`, `image build|push`, `deploy` and `agents` against a fake docker and a stub API.

Standard library only, no network, no Plow account, and -- the point of the
injected runner -- no docker daemon: every `docker` argv the CLI would run is
recorded and answered from a script.

    python3 tests/smoke-image-deploy.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

from plow_agents import cli, config  # noqa: E402

IMAGE = "ghcr.io/plow-pbc/reference"
SHA = "sha256:" + "ab" * 32
FREE, HELD = "ln_free", "ln_held"


class Stub(BaseHTTPRequestHandler):
    created: list[dict] = []
    lines = {"data": [{"uid": FREE, "display_name": "Free", "provider_key": "+15550001", "agent_uid": None},
                      {"uid": HELD, "display_name": "Held", "provider_key": "+15550002", "agent_uid": "agt_held"}]}
    chats = {"data": [{"status": "active", "participants": [{"type": "agent", "relationship": "self", "line": {"uid": uid}}]}
                      for uid in (FREE, HELD)]}
    agents = [
        {"uid": "agt_held", "provider": "exe:life", "line": {"uid": HELD}, "image": f"ghcr.io/plow-pbc/life@{SHA}",
         "status": "running", "failure_code": None},
        {"uid": "agt_direct", "provider": f"exe:{IMAGE}@{SHA}", "line": {"uid": FREE}, "image": f"{IMAGE}@{SHA}",
         "status": "failed", "failure_code": "image_pull_timeout"},
    ]

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
        if self.path == "/v1/lines":
            return self._send(200, Stub.lines)
        if self.path == "/v1/agents":
            return self._send(200, Stub.agents)
        self._send(404, {"detail": self.path})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/agents":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Stub.created.append(body)
            held = next((row for row in Stub.lines["data"] if row["uid"] == body["line_uid"]), {}).get("agent_uid")
            if held:
                return self._send(409, {"detail": {"code": "AGENT_EXISTS", "message": f"line already answers as {held}"}})
            agent = {"uid": "agt_new", "provider": body["provider"], "line": {"uid": body["line_uid"]}, "status": "provisioning"}
            return self._send(201, {"agent": agent, "token": None})
        self._send(404, {"detail": self.path})

    def log_message(self, *_: object) -> None:
        pass


class FakeDocker:
    """Every docker argv the CLI runs, and what it is told back."""

    def __init__(self, *, digest: str = SHA, fail: str | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.digest, self.fail = digest, fail

    def __call__(self, argv: list[str], env: dict[str, str]) -> tuple[int, str]:
        self.calls.append((argv, env))
        if self.fail is not None and self.fail in argv:
            return 1, ""
        if argv[1:3] == ["manifest", "inspect"]:
            return 0, json.dumps({"Ref": argv[-1], "Descriptor": {"digest": self.digest}})
        return 0, ""

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


def run(*argv: str, cwd: str, base: str, token: str, docker: FakeDocker | None = None) -> tuple[int, str, str]:
    """Drive the real Typer app in-process, with the docker seam replaced."""
    out, err = io.StringIO(), io.StringIO()
    full = ["plow-agents", "--api-base", base, "--token-file", token, *argv]
    original = os.getcwd()
    os.chdir(cwd)
    try:
        with patch.object(sys, "argv", full), patch.object(cli, "subprocess_runner", docker or FakeDocker()):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    cli.app()
                    code = 0
                except SystemExit as error:
                    code = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
                    if isinstance(error.code, str):
                        print(error.code, file=err)
    finally:
        os.chdir(original)
    return code, out.getvalue(), err.getvalue()


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
        toml = os.path.join(work, config.CONFIG_FILE)

        # --- plow-agents.toml: the unit of identity -------------------------
        code, _, _ = run("init", "--slug", "reference", "--image", IMAGE, cwd=work, base=base, token=token)
        check("init writes a repo, plow-agents.toml included",
              (code, os.path.isfile(toml), os.path.isfile(os.path.join(work, "agent.py"))), (0, True, True))
        check("toml read gives slug and image", (config.load(work).slug, config.load(work).image), ("reference", IMAGE))
        check("toml has no digest before a push", config.load(work).last_pushed, "")
        check("--image overrides the toml without writing it",
              (config.load(work, image="ghcr.io/other/x").image, config.load(work).image), ("ghcr.io/other/x", IMAGE))
        check("--slug overrides the toml", config.load(work, slug="other").slug, "other")
        code, _, err = run("init", cwd=work, base=base, token=token)
        check("init refuses to write over an existing repo", (code != 0, "does not merge" in err), (True, True))

        missing = os.path.join(work, "elsewhere")
        os.makedirs(missing)
        code, _, err = run("image", "push", cwd=missing, base=base, token=token)
        check("a verb with no toml names the field it wanted", (code != 0, "no image in" in err), (True, True))

        # --- image build ----------------------------------------------------
        docker = FakeDocker()
        code, _, _ = run("image", "build", cwd=work, base=base, token=token, docker=docker)
        check("build exits 0", code, 0)
        check("build is linux/amd64, tagged from the toml", docker.argvs,
              [["docker", "build", "--platform", "linux/amd64", "--tag", f"{IMAGE}:latest", "."]])

        docker = FakeDocker(fail="build")
        code, _, err = run("image", "build", cwd=work, base=base, token=token, docker=docker)
        check("a failed build is fatal", (code != 0, "build failed" in err), (True, True))

        # --- image push -----------------------------------------------------
        docker = FakeDocker()
        code, out, _ = run("image", "push", cwd=work, base=base, token=token, docker=docker)
        check("push exits 0", code, 0)
        check("push pushes the tag", docker.argvs[0], ["docker", "push", f"{IMAGE}:latest"])
        check("push reads the digest back with manifest inspect", docker.argvs[1],
              ["docker", "manifest", "inspect", "--verbose", f"{IMAGE}:latest"])
        check("push verifies the pull anonymously, with an empty docker config",
              bool(docker.calls[1][1].get("DOCKER_CONFIG")) and not os.listdir(docker.calls[1][1]["DOCKER_CONFIG"]), True)
        check("push prints the digest-pinned reference", out.strip(), f"{IMAGE}@{SHA}")
        check("push records last_pushed in the toml", config.load(work).last_pushed, SHA)
        with open(toml) as handle:
            check("and leaves the other keys alone", 'slug = "reference"' in handle.read(), True)

        docker = FakeDocker(digest="not-a-digest")
        code, _, err = run("image", "push", cwd=work, base=base, token=token, docker=docker)
        check("a registry answer with no sha256 digest is fatal", (code != 0, "no sha256 digest" in err), (True, True))
        check("and last_pushed is left as it was", config.load(work).last_pushed, SHA)

        second = "sha256:" + "cd" * 32
        run("image", "push", cwd=work, base=base, token=token, docker=FakeDocker(digest=second))
        check("a second push replaces last_pushed rather than appending", config.load(work).last_pushed, second)
        with open(toml) as handle:
            check("leaving exactly one last_pushed line", handle.read().count("last_pushed"), 1)
        # TOML ignores leading whitespace, so an indented key is the same key.
        indented = os.path.join(work, "indented", config.CONFIG_FILE)
        os.makedirs(os.path.dirname(indented))
        with open(indented, "w") as handle:
            handle.write(f'slug = "x"\nimage = "{IMAGE}"\n  last_pushed = "{SHA}"\n')
        config.record_last_pushed(indented, second)
        with open(indented) as handle:
            written = handle.read()
        check("an indented last_pushed is replaced, not duplicated", written.count("last_pushed"), 1)
        check("and the indentation is kept", '  last_pushed = ' in written, True)
        check("and the new digest is what the file now reads", config.load(os.path.dirname(indented)).last_pushed, second)
        config.record_last_pushed(toml, SHA)

        # --- deploy ---------------------------------------------------------
        Stub.created.clear()
        code, out, err = run("deploy", cwd=work, base=base, token=token)
        check("deploy exits 0", code, 0)
        check("deploy sends the digest-pinned image as the provider", Stub.created[-1],
              {"name": "reference", "line_uid": FREE, "provider": f"exe:{IMAGE}@{SHA}"})
        check("deploy prints the line and the reference it asked for", out.strip().split("\t"), ["agt_new", FREE, f"{IMAGE}@{SHA}"])
        check("deploy says requested, not deployed, and names the phase", ("Requested agent agt_new" in err, "provisioning" in err), (True, True))
        check("deploy does not claim the agent is up", "Deployed" in err, False)

        Stub.created.clear()
        explicit = "sha256:" + "ef" * 32
        run("deploy", explicit, "--line", FREE, cwd=work, base=base, token=token)
        check("a digest argument beats last_pushed", Stub.created[-1]["provider"], f"exe:{IMAGE}@{explicit}")

        Stub.created.clear()
        code, _, err = run("deploy", "latest", cwd=work, base=base, token=token)
        check("deploy refuses a tag", (code != 0, "not a sha256 digest" in err, Stub.created), (True, True, []))

        code, _, err = run("deploy", "--line", HELD, cwd=work, base=base, token=token)
        check("an occupied line is Plow's refusal, reported with what Plow said",
              (code != 0, "already answers as agt_held" in err), (True, True))

        held = dict(Stub.lines)
        Stub.lines = {"data": [dict(row, agent_uid="agt_x") for row in Stub.lines["data"]]}
        Stub.created.clear()
        code, _, err = run("deploy", cwd=work, base=base, token=token)
        check("deploy with no free line says so before creating anything",
              (code != 0, "no free line" in err, Stub.created), (True, True, []))
        Stub.lines = {"data": [dict(row, agent_uid=None) for row in held["data"]]}
        code, _, err = run("deploy", cwd=work, base=base, token=token)
        check("deploy with two free lines asks which", (code != 0, "more than one free line" in err), (True, True))
        Stub.lines = held

        empty = os.path.join(work, "no-toml")
        os.makedirs(empty)
        code, _, err = run("deploy", SHA, cwd=empty, base=base, token=token)
        check("deploy with no image configured names the field", (code != 0, "no image in" in err), (True, True))

        # --- agents ---------------------------------------------------------
        code, out, _ = run("agents", cwd=work, base=base, token=token)
        check("agents exits 0", code, 0)
        rows = {row.split("\t")[0]: row.split("\t") for row in out.splitlines()[1:]}
        check("agents heads its columns", out.splitlines()[0], "LINE\tSLUG\tSTATUS\tIMAGE")
        check("agents shows a listing deploy by slug, running", rows.get(HELD), [HELD, "life", "running", f"ghcr.io/plow-pbc/life@{SHA}"])
        # A digest is 71 characters. A table sized to the terminal ellipsised
        # it, which is the one field of this verb nobody can retype.
        check("agents shows a direct image deploy by its whole digest, and why it failed",
              rows.get(FREE), [FREE, "-", "failed (image_pull_timeout)", f"{IMAGE}@{SHA}"])

    server.shutdown()
    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
