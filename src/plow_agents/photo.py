"""The one request this tool makes that is not JSON in: the profile photo."""

from __future__ import annotations

import os
import urllib.parse

from .api import call, die

# Plow caps a profile photo at 5 MB and takes these four formats. Restated
# here only to refuse a file before any request is sent; the server decides.
MAX_PHOTO_BYTES = 5 * 1024 * 1024
PHOTO_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def is_hosted_url(value: str) -> bool:
    """Whether a `--photo` value names something already hosted, or a file here.

    The scheme decides, and schemes are case-insensitive: a prefix test that
    was not read `HTTPS://example.com/ada.jpg` as a filename, and went looking
    for it on disk. Anything urlsplit cannot parse is treated as a path, which
    is where an unparseable value gets the error that names it -- `cannot
    read`, with the value in it -- instead of a traceback.
    """
    try:
        return urllib.parse.urlsplit(value).scheme.lower() in ("http", "https")
    except ValueError:
        return False


def read_photo(path: str) -> bytes:
    """The file's bytes, refused here rather than over the network if unusable.

    Everything knowable without asking Plow is settled before the first
    request goes out, because `profile` may be writing a name too: a file that
    turns out to be unopenable, oversized, or not an image AFTER the name has
    landed is a command that half happened. Plow checks all of it again -- it
    has to, this is not its only client -- and stays the authority; this is
    only about not starting what cannot finish.
    """
    try:
        with open(path, "rb") as handle:
            content = handle.read(MAX_PHOTO_BYTES + 1)
    except OSError as error:
        die(f"cannot read {path}: {error}")
    if len(content) > MAX_PHOTO_BYTES:
        die(f"{path} is larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB")
    if not (content.startswith(PHOTO_SIGNATURES) or (content[:4] == b"RIFF" and content[8:12] == b"WEBP")):
        die(f"{path} is not a PNG, JPEG, GIF, or WebP image")
    return content


def upload_photo(api_base: str, token: str, path: str, content: bytes, name: str | None) -> object:
    """Send the image, and the display name with it. Returns the profile Plow answers with.

    One request, because that route writes both in the transaction that stores
    the bytes. Uploading and then PATCHing the name meant a failure at the
    second call left the photo stored and public beside the name it was sent
    to replace, and nothing here could say which half had happened.

    The part's own type is left as octet-stream: Plow reads the signature of
    the bytes rather than anything the uploader claims, so a guess here would
    only be a guess. httpx writes the part headers and percent-escapes the
    filename, so a quote or newline in it cannot rewrite the header around it.
    """
    return call(
        "POST",
        api_base,
        "/v1/auth/profile/photo",
        token=token,
        files={"file": (os.path.basename(path) or "photo", content, "application/octet-stream")},
        form={"display_name": name} if name is not None else None,
    )
