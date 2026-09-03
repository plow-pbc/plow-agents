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
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "plow-agents")

_spec = importlib.util.spec_from_loader("plow_agents", importlib.machinery.SourceFileLoader("plow_agents", CLI))
plow_agents = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plow_agents)

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


def compose(directory: str, project: str | None, *argv: str) -> None:
    """Compose against `directory`, either as `revoke` aims it or under a name."""
    aimed = ("docker", "compose", "--env-file", os.devnull, "-f", "./compose.yml", "--project-directory", ".")
    named = aimed if project is None else aimed + ("-p", project)
    subprocess.run(named + argv, cwd=directory, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def scratch(directory: str, dotenv: str = "") -> None:
    with open(os.path.join(directory, "compose.yml"), "w") as handle:
        handle.write(TRIVIAL)
    if dotenv:
        with open(os.path.join(directory, ".env"), "w") as handle:
            handle.write(dotenv)


def targeting_case() -> None:
    """A `.env` naming another project must not get that project torn down.

    The bystander's name is unique per invocation: this test tears its own
    fixtures down with `down -v`, and two runs sharing a name would delete each
    other's containers and volumes -- the very bug it exists to catch.
    """
    bystander = f"smoke-bystander-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory() as bystander_dir, tempfile.TemporaryDirectory() as agent_dir:
        scratch(bystander_dir)
        # The agent's own project, brought up the way `revoke` will aim at it,
        # so its name is this directory's -- and a `.env` pointing at the
        # bystander, which is the file this compose.yml invites you to keep.
        scratch(agent_dir, f"COMPOSE_PROJECT_NAME={bystander}\nCOMPOSE_FILE=/nowhere/compose.yml\n")
        agent_project = os.path.basename(agent_dir)
        compose(bystander_dir, bystander, "up", "-d")
        compose(agent_dir, None, "up", "-d")
        try:
            check("both projects' volumes exist before teardown", {f"{bystander}_home", f"{agent_project}_home"} <= volumes(), True)
            # The shell is hostile too, not only the `.env`.
            os.environ["COMPOSE_PROJECT_NAME"] = bystander
            os.environ["COMPOSE_FILE"] = "/nowhere/compose.yml"
            standing = plow_agents.compose_down(agent_dir)
            check("the agent's OWN volume is gone", f"{agent_project}_home" in volumes(), False)
            check("the bystander's volume SURVIVED", f"{bystander}_home" in volumes(), True)
            # The environment was hostile, so a bare success would be a claim
            # this teardown cannot support -- it says so instead.
            check("and the override is reported rather than claimed away", isinstance(standing, str) and "COMPOSE_PROJECT_NAME" in standing, True)
        finally:
            for key in ("COMPOSE_PROJECT_NAME", "COMPOSE_FILE"):
                os.environ.pop(key, None)
            for project, directory in ((bystander, bystander_dir), (agent_project, agent_dir)):
                subprocess.run(("docker", "compose", "--env-file", os.devnull, "-p", project, "down", "-v"), cwd=directory, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def override_case() -> None:
    """`.env` named the project at `up`, so a silent `down` proves nothing.

    Compose exits 0 for a project it was never pointed at, having removed
    nothing. The agent started here is under the operator's name and is still
    running; reporting "the agent is gone" would be false.
    """
    project = f"smoke-operator-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory() as work:
        scratch(work, f"COMPOSE_PROJECT_NAME={project}\n")
        # Up the documented way: plain `docker compose up -d`, `.env` honoured.
        subprocess.run(("docker", "compose", "up", "-d"), cwd=work, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            check("the operator's own project is up", f"{project}_home" in volumes(), True)
            standing = plow_agents.compose_down(work)
            check("teardown does NOT claim the agent is gone", isinstance(standing, str), True)
            check("and names where the project selector came from", ".env" in (standing or ""), True)
            check("and did not delete the operator's project behind their back", f"{project}_home" in volumes(), True)
        finally:
            subprocess.run(("docker", "compose", "--env-file", os.devnull, "-p", project, "down", "-v"), cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        scratch(work)
        path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.path.join(work, "nothing-here")
        try:
            standing = plow_agents.compose_down(work)
        except BaseException as error:  # noqa: BLE001 -- the point is that nothing escapes
            standing = f"RAISED {error!r}"
        finally:
            os.environ["PATH"] = path
        check("no docker on PATH is reported, not raised", isinstance(standing, str) and not standing.startswith("RAISED"), True)


def template_case() -> None:
    """A hand-copied template must not let `revoke` delete a live token.

    No stub API: `--api-base` points at a port nothing is listening on, so an
    attempt to revoke would fail as a network error naming that port. Getting
    the credential-file error instead is the proof that nothing was sent -- the
    key id is read, and refused, before anything reaches Plow.
    """
    with tempfile.TemporaryDirectory() as work:
        token = os.path.join(work, "token")
        with open(token, "w") as handle:
            handle.write("acct_stub\n")
        credential = os.path.join(work, "plow-credentials")
        shutil.copy(os.path.join(ROOT, "plow-credentials.example"), credential)
        with open(credential, "a") as handle:
            handle.write("\n")  # as an operator would, after filling in the token
        unreachable = "http://127.0.0.1:9"
        done = subprocess.run([sys.executable, CLI, "--api-base", unreachable, "--token-file", token, "revoke"], cwd=work, capture_output=True, text=True)
        check("revoke on a hand-copied template refuses", done.returncode != 0, True)
        check("and reaches Plow for nothing", unreachable not in done.stderr, True)
        check("and leaves the credential file in place", os.path.exists(credential), True)
        check("and says to use the dashboard", "dashboard" in done.stderr, True)
        check("the shipped template carries no key-id marker", plow_agents.KEY_ID_MARKER in open(os.path.join(ROOT, "plow-credentials.example")).read(), False)


def main() -> int:
    degraded_cases()
    if docker_alive():
        targeting_case()
        override_case()
    else:
        print("skip Docker is not running -- targeting case not exercised")
    template_case()
    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
