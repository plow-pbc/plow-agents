#!/usr/bin/env python3
"""`revoke`'s teardown hits this directory's project and no other.

Two things are proven here, both with real machinery rather than by reading the
code: a `.env` that names another project cannot redirect `docker compose down
-v` (real Docker, a real bystander project that has to survive), and a
hand-copied `plow-credentials.example` cannot make `revoke` report success over
a key that is still live (the real CLI against a stub API).

    python3 tests/smoke-teardown-targeting.py

The first half is skipped where Docker is not running.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "plow-agents")

_spec = importlib.util.spec_from_loader("plow_agents", importlib.machinery.SourceFileLoader("plow_agents", CLI))
plow_agents = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plow_agents)

BYSTANDER = "smoke-bystander-project"
TRIVIAL = "services:\n  a:\n    image: alpine\n    command: sleep 300\n    volumes:\n      - home:/home\nvolumes:\n  home:\n"

failures: list[str] = []


def check(what: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{what}\n  want: {want!r}\n  got:  {got!r}")
    print(f"{'ok  ' if got == want else 'FAIL'} {what}")


def docker_alive() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(("docker", "info"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def volumes() -> set[str]:
    out = subprocess.run(("docker", "volume", "ls", "--format", "{{.Name}}"), capture_output=True, text=True)
    return set(out.stdout.split())


def targeting_case() -> None:
    """A `.env` naming another project must not get that project torn down."""
    with tempfile.TemporaryDirectory() as bystander_dir, tempfile.TemporaryDirectory() as agent_dir:
        for directory in (bystander_dir, agent_dir):
            with open(os.path.join(directory, "compose.yml"), "w") as handle:
                handle.write(TRIVIAL)
        # A real bystander project, up, with a volume of its own.
        subprocess.run(("docker", "compose", "-p", BYSTANDER, "up", "-d"), cwd=bystander_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # The agent's own directory, up -- and a `.env` aimed at the bystander,
        # which is exactly what this compose.yml tells operators to keep here.
        with open(os.path.join(agent_dir, ".env"), "w") as handle:
            handle.write(f"COMPOSE_PROJECT_NAME={BYSTANDER}\nCOMPOSE_FILE=/nowhere/compose.yml\n")
        subprocess.run(("docker", "compose", "--env-file", os.devnull, "-p", "smoke-agent-project", "up", "-d"), cwd=agent_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            check("the bystander's volume exists before teardown", f"{BYSTANDER}_home" in volumes(), True)
            # The environment is hostile too, not only the `.env`.
            os.environ["COMPOSE_PROJECT_NAME"] = BYSTANDER
            os.environ["COMPOSE_FILE"] = "/nowhere/compose.yml"
            standing = plow_agents.compose_down(agent_dir)
            check("teardown reports the project gone", standing, None)
            check("the bystander's volume SURVIVED the teardown", f"{BYSTANDER}_home" in volumes(), True)
        finally:
            for key in ("COMPOSE_PROJECT_NAME", "COMPOSE_FILE"):
                os.environ.pop(key, None)
            for project, directory in ((BYSTANDER, bystander_dir), ("smoke-agent-project", agent_dir)):
                subprocess.run(("docker", "compose", "--env-file", os.devnull, "-p", project, "down", "-v"), cwd=directory, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Stub(BaseHTTPRequestHandler):
    deletes: list[str] = []

    def do_DELETE(self) -> None:  # noqa: N802
        Stub.deletes.append(self.path)
        raw = json.dumps({"status": "revoked"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_: object) -> None:
        pass


def template_case() -> None:
    """A hand-copied template must not let `revoke` delete a live token."""
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with tempfile.TemporaryDirectory() as work:
        token = os.path.join(work, "token")
        with open(token, "w") as handle:
            handle.write("acct_stub\n")
        credential = os.path.join(work, "plow-credentials")
        shutil.copy(os.path.join(ROOT, "plow-credentials.example"), credential)
        with open(credential, "a") as handle:
            handle.write("\n")  # as an operator would, after filling in the token
        done = subprocess.run([sys.executable, CLI, "--api-base", base, "--token-file", token, "revoke"], cwd=work, capture_output=True, text=True)
        check("revoke on a hand-copied template refuses", done.returncode != 0, True)
        check("and revokes nothing", Stub.deletes, [])
        check("and leaves the credential file in place", os.path.exists(credential), True)
        check("and says to use the dashboard", "dashboard" in done.stderr, True)
    server.shutdown()


def degraded_cases() -> None:
    """Neither missing file nor missing docker may raise: the key is already gone."""
    with tempfile.TemporaryDirectory() as empty:
        standing = plow_agents.compose_down(empty)
        check("a directory with no compose.yml is reported, not raised", isinstance(standing, str) and "compose.yml" in standing, True)
        check("and the note carries the aimed recovery command", "--env-file" in (standing or "") and "-u COMPOSE_PROJECT_NAME" in (standing or ""), True)
        check("and never the bare command the tool refuses to run", "docker compose down -v" not in (standing or "").replace(plow_agents.RECOVERY, ""), True)
    # `docker` off PATH entirely: the subprocess raises, and compose_down owes
    # the caller a note rather than a traceback on top of a revoked key.
    with tempfile.TemporaryDirectory() as work:
        with open(os.path.join(work, "compose.yml"), "w") as handle:
            handle.write(TRIVIAL)
        path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.path.join(work, "nothing-here")
        try:
            standing = plow_agents.compose_down(work)
        except BaseException as error:  # noqa: BLE001 -- the point is that nothing escapes
            standing = f"RAISED {error!r}"
        finally:
            os.environ["PATH"] = path
        check("no docker on PATH is reported, not raised", isinstance(standing, str) and not standing.startswith("RAISED"), True)


def main() -> int:
    degraded_cases()
    if docker_alive():
        targeting_case()
    else:
        print("skip Docker is not running -- targeting case not exercised")
    template_case()
    print("ok   the shipped template carries no key-id marker" if plow_agents.KEY_ID_MARKER not in open(os.path.join(ROOT, "plow-credentials.example")).read() else "FAIL template still carries a key-id marker")
    if plow_agents.KEY_ID_MARKER in open(os.path.join(ROOT, "plow-credentials.example")).read():
        failures.append("plow-credentials.example still carries a key-id marker")
    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
