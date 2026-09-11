"""Talking to Plow, and the two files this tool keeps on disk.

`login` stores an ACCOUNT token in ~/.config/plow/token; it can list lines,
mint, rotate, revoke, deploy, and read or set the public profile, so it does
travel -- over HTTPS, to Plow. What it never does is enter a container. `mint`
uses it for an AGENT credential scoped to one line, and that second one is the
only credential a container ever sees.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import NoReturn

DEFAULT_API_BASE = "https://api.plow.co"
CREDENTIAL_FILE = "plow-credentials"


def log(message: str = "") -> None:
    print(message, file=sys.stderr, flush=True)


def die(message: str) -> NoReturn:
    raise SystemExit(f"plow-agents: {message}")


def request(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, object]:
    """One call, JSON in by default. Returns (status, parsed body); never raises on HTTP status.

    `data`/`content_type` are for the one request that is not JSON in: the
    profile photo upload, which sends the file itself.
    """
    headers = {"Accept": "application/json"}
    if data is None and body is not None:
        data, content_type = json.dumps(body).encode(), "application/json"
    if content_type is not None:
        headers["Content-Type"] = content_type
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # HTTP is restricted to development hosts; their credentials must bypass proxies.
    open_url = urllib.request.build_opener(urllib.request.ProxyHandler({})).open if req.type == "http" else urllib.request.urlopen
    try:
        with open_url(req, timeout=30) as response:
            raw, status = response.read(), response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    except OSError as error:
        die(f"cannot reach {url}: {error}")
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def call(method: str, base: str, path: str, **kwargs) -> object:
    """The same call, with a non-2xx as a fatal error rather than a value."""
    status, payload = request(method, base + path, **kwargs)
    if status // 100 != 2:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("message")
        die(f"{method} {path} answered {status}" + (f": {detail}" if detail else ""))
    return payload


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def write_private(path: str, body: str) -> str:
    """Write a mode-600 file atomically. Returns the absolute path.

    The temporary name is unpredictable and created O_EXCL|O_NOFOLLOW, the mode
    is checked rather than assumed, and the rename is last -- so a failed run
    leaves the previous credential intact.
    """
    destination = os.path.abspath(path)
    directory = os.path.dirname(destination) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".plow-agents.", suffix=".new")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(body)
        if stat.S_IMODE(os.stat(temporary).st_mode) != 0o600:
            die("refusing to install a credential that is not mode 600")
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


def read_credential(path: str) -> dict[str, str]:
    """Read the self-hosted agent identity and its container's API address."""
    try:
        with open(path) as handle:
            credential = {}
            for raw in handle:
                line = raw.strip()
                if line.startswith("# plow-agent-uid:"):
                    key, value = "agent_uid", line.partition(":")[2].strip()
                elif line.startswith("#") or "=" not in line:
                    continue
                else:
                    key, value = line.split("=", 1)
                    if key not in ("PLOW_API_BASE", "PLOW_AGENT_TOKEN"):
                        continue
                if key == "agent_uid" and key in credential and credential[key] != value:
                    die("conflicting agent UIDs in credential -- resolve before continuing")
                credential[key] = value
            return credential
    except FileNotFoundError:
        return {}


def credential_agent_uid(credential: dict[str, str]) -> str:
    uid = credential.get("agent_uid", "")
    if not uid:
        die("no valid # plow-agent-uid: comment in plow-credentials -- use `revoke <line>` to retire the agent")
    return uid


def install_credential(path: str, uid: str, token: str, base: str) -> None:
    if any(character in token + base + uid for character in "\r\n\"\\"):
        die("the credential carries quote or newline characters")
    written = write_private(path, f"PLOW_API_BASE={base}\nPLOW_AGENT_TOKEN={token}\n# plow-agent-uid: {uid}\n")
    log(f"Wrote {written} (mode 600).")


def strip_v1(base: str) -> str:
    """Validate an API root before requests, then remove an optional `/v1`."""
    if not base or any(character in base for character in "\r\n\"\\"):
        die("API base must be nonempty and contain no quotes, backslashes, or newlines")
    base = base.rstrip("/")
    base = base[: -len("/v1")] if base.endswith("/v1") else base
    if not base:
        die("API base must not be empty after normalization")
    return base


def checked_api_base(base: str) -> str:
    """The API root this tool calls, refused here if a secret would leave in clear."""
    # The agent builds `${PLOW_API_BASE}/v1`, so a /v1 here would 404 every call.
    base = strip_v1(base)
    endpoint = urllib.parse.urlsplit(base)
    host = endpoint.hostname or ""
    local = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".orb.local")
    if not host or not (endpoint.scheme == "https" or endpoint.scheme == "http" and local):
        die("--api-base requires HTTPS except for localhost, 127.0.0.1, ::1, or *.orb.local")
    return base


def token_path(override: str | None) -> str:
    if override:
        return os.path.abspath(override)
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(config, "plow", "token")


def account_token(override: str | None) -> str:
    path = token_path(override)
    try:
        with open(path) as handle:
            token = handle.read().strip()
    except OSError:
        die(f"no account token at {path} -- run `plow-agents login` first")
    if not token:
        die(f"{path} is empty -- run `plow-agents login` again")
    return token


def account_lines(base: str, token: str) -> list[dict]:
    """Only lines on which this account owns an active chat can be minted."""
    chats = call("GET", base, "/v1/chats", token=token)["data"]
    owned = {
        participant["line"]["uid"]
        for chat in chats if chat["status"] == "active"
        for participant in chat["participants"]
        if participant["type"] == "agent" and participant["relationship"] == "self"
    }
    if not owned:
        return []
    return [line for line in call("GET", base, "/v1/lines", token=token)["data"] if line["uid"] in owned]
