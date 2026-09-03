#!/usr/bin/env python3
"""`provider` against a real directory: what lands in `.env`, and what does not.

Standard library only, no network, no Plow account, no container -- the file is
the whole contract. What is under test is the three things that are easy to get
wrong: the secret comes from the environment and never from `argv`, the lines
this tool does not own are left alone, and the mode is 600.

    python3 tests/smoke-provider-env.py
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile

CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "plow-agents")
KEY = "sk-ant-not-a-real-key-0123456789"

failures: list[str] = []


def check(what: str, got, want) -> None:
    if got != want:
        failures.append(f"FAIL {what}\n  got  {got!r}\n  want {want!r}")
    else:
        print(f"ok   {what}")


def run(*argv: str, cwd: str, env: dict | None = None) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.update(env or {})
    return subprocess.run([sys.executable, CLI, "provider", *argv], cwd=cwd, env=environment, capture_output=True, text=True)


def env_lines(work: str) -> list[str]:
    with open(os.path.join(work, ".env")) as handle:
        return handle.read().splitlines()


def main() -> int:
    with tempfile.TemporaryDirectory() as work:
        # A `.env` that already carries something of the user's.
        with open(os.path.join(work, ".env"), "w") as handle:
            handle.write("PLOW_AGENT_REPO=/somewhere\n# a comment of my own\n")

        missing = run("anthropic", "claude-sonnet-5", cwd=work)
        check("no key in the environment is a refusal", missing.returncode, 1)
        check("and it names the variable to set", "$ANTHROPIC_API_KEY" in missing.stderr, True)
        check("and it wrote nothing", env_lines(work), ["PLOW_AGENT_REPO=/somewhere", "# a comment of my own"])

        written = run("anthropic", "claude-sonnet-5", cwd=work, env={"ANTHROPIC_API_KEY": KEY})
        check("with the key exported it writes", written.returncode, 0)
        check(
            "the three names it owns, after the lines it does not",
            env_lines(work),
            [
                "PLOW_AGENT_REPO=/somewhere",
                "# a comment of my own",
                "# plow-agents-key-env: ANTHROPIC_API_KEY",
                "HERMES_PROVIDER=anthropic",
                "HERMES_MODEL=claude-sonnet-5",
                f"ANTHROPIC_API_KEY={KEY}",
            ],
        )
        check("mode 600", stat.S_IMODE(os.stat(os.path.join(work, ".env")).st_mode), 0o600)
        # The key is what this whole verb exists to keep out of sight: it must
        # not be echoed back, and it was never on the command line to begin with.
        check("the key is not printed", KEY in written.stdout + written.stderr, False)

        again = run("anthropic", "claude-opus-5", cwd=work, env={"ANTHROPIC_API_KEY": KEY})
        check("setting it again replaces rather than appends", again.returncode, 0)
        check("one model line, the new one", [line for line in env_lines(work) if line.startswith("HERMES_MODEL=")], ["HERMES_MODEL=claude-opus-5"])

        shown = run(cwd=work)
        check("`provider` alone reports the provider", "HERMES_PROVIDER=anthropic" in shown.stderr, True)
        check("and redacts the key", KEY in shown.stderr, False)
        check("saying only how long it is", f"ANTHROPIC_API_KEY=<set, {len(KEY)} characters>" in shown.stderr, True)

        named = run("someprovider", "some-model", "--key-env", "MY_OWN_KEY", cwd=work, env={"MY_OWN_KEY": KEY})
        check("--key-env names the variable", named.returncode, 0)
        check("and the file names it too", f"MY_OWN_KEY={KEY}" in env_lines(work), True)

        removed = run("--unset", cwd=work)
        check("--unset takes them back out", removed.returncode, 0)
        check("leaving what was not this tool's", env_lines(work), ["PLOW_AGENT_REPO=/somewhere", "# a comment of my own"])

    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
