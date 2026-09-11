"""Docker, driven as a subprocess, through one seam the tests replace.

Nothing here shells out through a string: every command is an argv list, so a
registry host or a tag out of the config file can never become shell syntax.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable

from .api import die, log

# (argv, env overrides) -> (exit status, stdout). The default runs docker; the
# tests pass their own and no daemon is touched.
Runner = Callable[[list[str], dict[str, str]], "tuple[int, str]"]


def subprocess_runner(argv: list[str], env: dict[str, str]) -> tuple[int, str]:
    log("$ " + " ".join(argv))
    finished = subprocess.run(argv, env={**os.environ, **env}, capture_output=True, text=True)
    if finished.stderr:
        log(finished.stderr.rstrip())
    return finished.returncode, finished.stdout


def run(runner: Runner, argv: list[str], *, env: dict[str, str] | None = None, what: str) -> str:
    status, stdout = runner(argv, env or {})
    if status != 0:
        die(f"{what} failed: `{' '.join(argv)}` exited {status}")
    return stdout


def anonymous_env() -> dict[str, str]:
    """A docker config with no credentials in it, so a pull is really anonymous.

    Plow pulls these images with no login at all. Reading the digest back
    through the pushing account's own keychain would confirm the image exists
    for *you* and say nothing about whether Plow can boot it.
    """
    return {"DOCKER_CONFIG": tempfile.mkdtemp(prefix="plow-agents-anon-")}
