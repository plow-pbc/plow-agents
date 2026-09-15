"""The repo `init` writes: a reference agent, a Dockerfile, an Action, a toml.

The files live beside this module as `template/` and ship in the wheel, so
`init` is a copy with two substitutions rather than a pile of here-documents.
"""

from __future__ import annotations

import os
from importlib import resources

from .api import die

TEMPLATE = "template"


def copy_into(destination: str, *, slug: str = "", image: str = "") -> list[str]:
    """Copy the template, refusing to write over anything. Returns what was written."""
    source = resources.files(__package__).joinpath(TEMPLATE)
    files = sorted(_walk(str(source)))
    if not files:
        die("the template is missing from this installation")
    # A checkout can hold symlinks: a target that resolves outside the
    # destination, or any link at all where a file would go, is not written.
    root = os.path.realpath(destination)
    targets = {name: os.path.join(root, name) for name in files}
    escaping = [name for name, target in targets.items() if not os.path.realpath(target).startswith(root + os.sep)]
    if escaping:
        die(f"{os.path.join(destination, escaping[0])} resolves outside {destination} -- init does not write through a symlink")
    written = []
    for name, target in targets.items():
        body = _read(os.path.join(str(source), name))
        if name == "plow-agents.toml":
            body = _filled(body, slug=slug, image=image)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # `xb`, so the only thing standing between a file and this write is the
        # kernel: a scan first and a write after is a window in which an editor,
        # a checkout or a second `init` can put a file there to be truncated.
        with open(target, "xb") as handle:
            handle.write(body)
        written.append(os.path.join(destination, name))
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


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _filled(body: bytes, *, slug: str, image: str) -> bytes:
    """The toml with its two fields substituted, before anything is written."""
    return body.replace(b'slug = ""', f'slug = "{slug}"'.encode()).replace(b'image = ""', f'image = "{image}"'.encode())
