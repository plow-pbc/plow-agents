"""`plow-agents.toml`: one repo, one agent, one listing.

    slug  = "life"
    image = "ghcr.io/plow-pbc/life"
    last_pushed = "sha256:..."   # written by image push

Read from the working directory by every `image` and `listing` verb. `--slug`
and `--image` override without touching the file.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass

from .api import die

CONFIG_FILE = "plow-agents.toml"


@dataclass(frozen=True)
class Config:
    path: str
    slug: str = ""
    image: str = ""
    last_pushed: str = ""

    def need_image(self) -> str:
        if not self.image:
            die(f"no image in {self.path} -- set `image = \"ghcr.io/you/agent\"` or pass --image")
        return self.image

    def need_slug(self) -> str:
        if not self.slug:
            die(f"no slug in {self.path} -- set `slug = \"your-agent\"` or pass --slug")
        return self.slug

    def need_last_pushed(self) -> str:
        if not self.last_pushed:
            die(f"no digest given and no last_pushed in {self.path} -- run `plow-agents image push` first")
        return self.last_pushed


def load(directory: str = ".", *, slug: str | None = None, image: str | None = None) -> Config:
    """The file in `directory`, with the two overrides applied.

    A missing file is not an error here: the verb that needs a field says so,
    naming the field, which is more use than "no config" from a tool whose
    whole job the caller is in the middle of.
    """
    path = os.path.abspath(os.path.join(directory, CONFIG_FILE))
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raw = {}
    except tomllib.TOMLDecodeError as error:
        die(f"{path} is not valid TOML: {error}")
    fields = {key: raw.get(key, "") for key in ("slug", "image", "last_pushed")}
    for key, value in fields.items():
        if not isinstance(value, str):
            die(f"{path}: `{key}` must be a string")
    if slug is not None:
        fields["slug"] = slug
    if image is not None:
        fields["image"] = image
    return Config(path=path, **fields)


def record_last_pushed(path: str, digest: str) -> None:
    """Rewrite one key in place, leaving every other line and comment alone.

    A read-modify-dump through a TOML writer would reformat a file the person
    edits by hand; this is the only key the CLI ever writes.
    """
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        die(f"refusing to record a digest that is not sha256:<64 hex>: {digest}")
    line = f'last_pushed = "{digest}"'
    try:
        with open(path) as handle:
            body = handle.read()
    except FileNotFoundError:
        body = ""
    replaced, count = re.subn(r"(?m)^last_pushed\s*=.*$", line, body)
    if not count:
        replaced = (body.rstrip("\n") + "\n" if body.strip() else "") + line + "\n"
    with open(path, "w") as handle:
        handle.write(replaced)
