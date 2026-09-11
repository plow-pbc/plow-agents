"""Build one image for exe.dev, push it, and read back the digest Plow will pull.

The tag is a transport handle and nothing else: it is how the registry is
addressed for the push, and it is thrown away the moment the digest is known.
Every reference that reaches Plow is `name@sha256:...`.
"""

from __future__ import annotations

import json

from .api import die
from .docker import Runner, anonymous_env, run

PLATFORM = "linux/amd64"
DEFAULT_TAG = "latest"


def build(runner: Runner, *, image: str, tag: str, context: str) -> str:
    """Build for the one architecture exe.dev runs. Returns the tagged reference."""
    reference = f"{image}:{tag}"
    run(runner, ["docker", "build", "--platform", PLATFORM, "--tag", reference, context], what="build")
    return reference


def push(runner: Runner, *, image: str, tag: str) -> str:
    """Push, then read the digest back the way Plow will: anonymously. Returns `sha256:...`."""
    reference = f"{image}:{tag}"
    run(runner, ["docker", "push", reference], what="push")
    return anonymous_digest(runner, reference)


def anonymous_digest(runner: Runner, reference: str) -> str:
    """The digest of `reference`, read with no credentials at all.

    This is the verification as well as the lookup: Plow pulls with no login,
    so a manifest that only resolves through the pushing account's keychain is
    a manifest Plow cannot boot, and this call is the one that says so.
    """
    stdout = run(
        runner,
        ["docker", "manifest", "inspect", "--verbose", reference],
        env=anonymous_env(),
        what=f"anonymous pull of {reference}",
    )
    try:
        manifest = json.loads(stdout)
    except ValueError:
        die(f"docker manifest inspect {reference} did not answer JSON")
    if isinstance(manifest, list):
        die(f"{reference} is a multi-architecture index; build it for {PLATFORM} alone so one digest names it")
    digest = manifest.get("Descriptor", {}).get("digest") if isinstance(manifest, dict) else None
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        die(f"docker manifest inspect {reference} returned no sha256 digest")
    return digest
