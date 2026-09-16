"""Build one image for exe.dev, push it, and prove Plow can pull the digest.

The tag is a transport handle and nothing else: it is how the registry is
addressed for the push, and it is thrown away the moment the digest is known.
Every reference that reaches Plow is `name@sha256:...`.
"""

from __future__ import annotations

import re
import urllib.request

import httpx

from . import api
from .api import die
from .docker import Runner, run

PLATFORM = "linux/amd64"
DEFAULT_TAG = "latest"
# The last line of `docker push`: `<tag>: digest: sha256:<hex> size: <n>`.
PUSHED_DIGEST = re.compile(r"digest: (sha256:[0-9a-f]{64})")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
# A registry whose Bearer realm is on a host other than its own.
TOKEN_HOSTS = {"registry-1.docker.io": "auth.docker.io"}


def build(runner: Runner, *, image: str, tag: str) -> str:
    """Build this directory for the one architecture exe.dev runs. Returns the tagged reference."""
    reference = f"{image}:{tag}"
    run(runner, ["docker", "build", "--platform", PLATFORM, "--tag", reference, "."], what="build")
    return reference


def push(runner: Runner, *, image: str, tag: str) -> str:
    """Push, then pull the digest's manifest the way Plow will: anonymously. Returns `sha256:...`."""
    reference = f"{image}:{tag}"
    found = PUSHED_DIGEST.findall(run(runner, ["docker", "push", reference], what="push"))
    if not found:
        die(f"docker push {reference} printed no sha256 digest")
    digest = found[-1]
    verify_public(image, digest)
    return digest


def verify_public(image: str, digest: str) -> None:
    """Fetch `image@digest`'s manifest with no credentials at all, or die saying why not.

    This is plain registry HTTP rather than a `docker` call on purpose: docker
    finds credentials on its own -- on macOS an empty `DOCKER_CONFIG` still
    falls back to the keychain -- so a docker pull that succeeds says nothing
    about whether Plow, which has no login, can boot the image. Here the only
    token ever presented is the one the registry hands to anybody who asks.
    """
    host, name = registry_of(image)
    url = f"https://{host}/v2/{name}/manifests/{digest}"
    accept = {"Accept": ", ".join(MANIFEST_TYPES + INDEX_TYPES)}
    # No `trust_env`: it would read ~/.netrc, and a credential from there would
    # make this pull no longer anonymous.
    try:
        with httpx.Client(transport=api.TRANSPORT, trust_env=False, timeout=api.TIMEOUT_S) as client:
            response = client.get(url, headers=accept)
            if response.status_code == 401:
                token = _anonymous_token(client, response.headers.get("WWW-Authenticate", ""), host, name)
                if token:
                    response = client.get(url, headers={**accept, "Authorization": f"Bearer {token}"})
    except httpx.HTTPError as error:
        die(f"cannot reach {host} to check {image}@{digest} pulls anonymously: {error}")
    if response.status_code in (401, 403):
        die(f"{image}@{digest} is not public: the registry refused an anonymous pull (HTTP {response.status_code}). "
            "Plow pulls with no login -- make the package public, then run `plow-agents image push` again.")
    if response.status_code != 200:
        die(f"an anonymous pull of {image}@{digest} answered HTTP {response.status_code}")
    if response.headers.get("Content-Type", "").split(";")[0].strip() in INDEX_TYPES:
        die(f"{image}@{digest} is a multi-architecture index; build it for {PLATFORM} alone so one digest names it")


def registry_of(image: str) -> tuple[str, str]:
    """(registry host, repository name), by docker's own rule for which is which."""
    first, _, rest = image.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost") and first != "docker.io":
        return first, rest
    # Docker Hub: `docker.io` is its name, not its API host, and a one-part
    # repository is an official image under `library/`.
    name = rest if first == "docker.io" and rest else image
    return "registry-1.docker.io", name if "/" in name else f"library/{name}"


def _anonymous_token(client: httpx.Client, challenge: str, host: str, name: str) -> str | None:
    """The pull token a Bearer challenge's realm gives to a caller with no credentials, if it gives one.

    The realm is the registry's to name, so it is only followed over https to
    the registry's own host -- or Docker Hub's, whose tokens come from another.
    """
    scheme, _, params = challenge.partition(" ")
    if scheme.lower() != "bearer":
        return None
    fields = urllib.request.parse_keqv_list(urllib.request.parse_http_list(params))
    realm = fields.get("realm")
    if not realm:
        return None
    # `host` is an authority and keeps an explicit port, `:443` included; both
    # sides compare as (host, port) with https's default filled in.
    parsed, wanted = httpx.URL(realm), httpx.URL(f"https://{TOKEN_HOSTS.get(host, host)}")
    if parsed.scheme != "https" or (parsed.host, parsed.port or 443) != (wanted.host, wanted.port or 443):
        die(f"{host} asked for a token from {realm} -- refusing a realm that is not https on {TOKEN_HOSTS.get(host, host)}")
    query = {"service": fields.get("service", ""), "scope": fields.get("scope") or f"repository:{name}:pull"}
    answer = client.get(realm, params={key: value for key, value in query.items() if value})
    if answer.status_code != 200:
        return None
    try:
        body = answer.json()
    except ValueError:
        return None
    return body.get("token") or body.get("access_token") if isinstance(body, dict) else None
