"""The repo `init` writes: a reference agent, a Dockerfile, an Action, a toml.

The files live beside this module as `template/` and ship in the wheel, so
`init` is a copy with two substitutions rather than a pile of here-documents.
"""

from __future__ import annotations

import os
import shutil
from importlib import resources

from .api import die

TEMPLATE = "template"


def copy_into(destination: str, *, slug: str = "", image: str = "") -> list[str]:
    """Copy the template, refusing to write over anything. Returns what was written."""
    source = resources.files(__package__).joinpath(TEMPLATE)
    files = sorted(_walk(str(source)))
    if not files:
        die("the template is missing from this installation")
    existing = [name for name in files if os.path.exists(os.path.join(destination, name))]
    if existing:
        die(f"{destination} already has {', '.join(existing[:3])} -- init writes a new repo, it does not merge")
    written = []
    for name in files:
        target = os.path.join(destination, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(str(source), name), target)
        written.append(target)
    _fill(os.path.join(destination, "plow-agents.toml"), slug=slug, image=image)
    return written


def _walk(root: str) -> list[str]:
    """Every file in the template, as paths relative to its root."""
    found = []
    for directory, _, names in os.walk(root):
        for name in names:
            if name.endswith((".pyc", ".pyo")) or "__pycache__" in directory:
                continue
            found.append(os.path.relpath(os.path.join(directory, name), root))
    return found


def _fill(path: str, *, slug: str, image: str) -> None:
    with open(path) as handle:
        body = handle.read()
    body = body.replace('slug = ""', f'slug = "{slug}"').replace('image = ""', f'image = "{image}"')
    with open(path, "w") as handle:
        handle.write(body)
