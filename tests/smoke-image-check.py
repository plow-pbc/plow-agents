#!/usr/bin/env python3
"""`image check` and `init`, against a fake docker and a real stub server.

No docker daemon and no network beyond loopback. The fake runner answers the
`docker` argv the CLI would run, and on `docker start` it launches a tiny
agent **in this process** that reads the credential the CLI actually wrote and
talks to the real stub over a real WebSocket. So the stub, the frames and the
assertion order are exercised; only the container is fake.

    python3 tests/smoke-image-check.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

from websockets.sync.client import connect  # noqa: E402

from plow_agents import check, template  # noqa: E402
from plow_agents.stub import PROMPT  # noqa: E402

TEMPLATE = os.path.join(SRC, "plow_agents", "template")
CONTAINER = "c0ffee"
COMPLIANT = {"Cmd": ["python3", "/opt/agent/agent.py"], "ExposedPorts": {}}


def call(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"null")


def fake_agent(credentials: str, *, skip: str | None, stopping: threading.Event, heard: list[str]) -> None:
    """What a compliant image does, with one step removable to make it not.

    Reads the file the CLI wrote, exactly as a container would; the host name
    `--add-host` would resolve is resolved here instead.
    """
    values = dict(line.split("=", 1) for line in credentials.splitlines() if "=" in line)
    base = values["PLOW_API_BASE"].replace("host.docker.internal", "127.0.0.1")
    token = values["PLOW_AGENT_TOKEN"]
    try:
        if skip == "identity":
            return
        call("GET", f"{base}/v1/agents/cloud/me", "wrong-token" if skip == "token" else token)
        if skip in ("token", "websocket"):
            return
        ticket = call("POST", f"{base}/v1/ws/ticket", token, {})["ticket"]
        with connect(f"{base.replace('http', 'ws', 1)}/v1/ws?ticket={ticket}") as socket:
            while not stopping.is_set():
                frame = json.loads(socket.recv(timeout=10))
                if frame.get("event_type") != "message_received":
                    continue
                if skip == "reply":
                    return
                body = frame["data"]["message"]["body"]
                heard.append(body)
                call("POST", f"{base}/v1/chats/{frame['chat_id']}/messages", token,
                     {"body": f"ack: {body[:40]}"})
                return
    except Exception as error:  # noqa: BLE001 -- a fake agent that dies is a failing check
        print(f"    (fake agent stopped: {type(error).__name__}: {error})")


class FakeDocker:
    """Every docker argv `check` runs, answered from a script."""

    def __init__(self, *, config: dict | None = None, skip: str | None = None,
                 uids: tuple[str, ...] = ("0", "10000"), exit_code: int = 0) -> None:
        self.argvs: list[list[str]] = []
        self.config = COMPLIANT if config is None else config
        self.skip, self.uids, self.exit_code = skip, uids, exit_code
        self.stopping = threading.Event()
        self.agent: threading.Thread | None = None
        # Kept rather than pointed at: the CLI stages the credential in a
        # temporary directory it deletes when the check returns.
        self.credentials = ""
        self.credential_mode = 0
        self.cp: list[str] = []
        self.heard: list[str] = []

    def __call__(self, argv: list[str], env: dict[str, str]) -> tuple[int, str]:
        self.argvs.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return 0, json.dumps(self.config)
        if argv[1] == "create":
            return 0, CONTAINER + "\n"
        if argv[1] == "cp":
            self.cp = argv
            staged = os.path.join(argv[2], "credentials")
            with open(staged) as handle:
                self.credentials = handle.read()
            self.credential_mode = os.stat(staged).st_mode & 0o777
            return 0, ""
        if argv[1] == "start":
            self.agent = threading.Thread(
                target=fake_agent, args=(self.credentials,),
                kwargs={"skip": self.skip, "stopping": self.stopping, "heard": self.heard}, daemon=True)
            self.agent.start()
            return 0, ""
        if argv[1] == "top":
            return 0, "UID                 COMMAND\n" + "".join(f"{uid}    python3 agent.py\n" for uid in self.uids)
        if argv[1] == "stop":
            self.stopping.set()
            return 0, ""
        if argv[1] == "inspect":
            return 0, f"{self.exit_code}\n"
        return 0, ""


def main() -> int:
    failures = []

    def check_that(what: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{what}\n  want: {want!r}\n  got:  {got!r}")
        print(f"{'ok  ' if got == want else 'FAIL'} {what}")

    def run_check(docker: FakeDocker) -> tuple[list[str] | None, check.ContractError | None]:
        try:
            return check.check(docker, image="ghcr.io/you/agent:latest", agent_id="demo", timeout=10), None
        except check.ContractError as failure:
            return None, failure

    # --- a compliant image passes every assertion ----------------------------
    docker = FakeDocker()
    passed, failed = run_check(docker)
    check_that("check passes on a compliant agent", failed and failed.assertion, None)
    check_that("and names every assertion it made", passed, [
        "the image has a CMD to run as PID 1",
        "the image declares no listening ports",
        "the agent calls GET /v1/agents/cloud/me with its token",
        "the agent presents the token from the credentials file",
        "the agent runs as uid 10000",
        "the agent opens the chat WebSocket",
        "the agent replies to one message",
        "the agent exits cleanly on SIGTERM",
    ])
    check_that("the credential is written as the contract's three lines",
               sorted(line.split("=")[0] for line in docker.credentials.splitlines()),
               ["AGENT_ID", "PLOW_AGENT_TOKEN", "PLOW_API_BASE"])
    check_that("and mode 600, as Plow writes it", oct(docker.credential_mode), "0o600")
    check_that("and names the container by the host it can reach this machine on",
               "PLOW_API_BASE=http://host.docker.internal:" in docker.credentials, True)
    check_that("it is copied in as root rather than bind-mounted, so the mode is Plow's",
               (docker.cp[1], docker.cp[3]), ("cp", f"{CONTAINER}:/var/lib/"))
    check_that("the container is created with no command override",
               [argv for argv in docker.argvs if argv[1] == "create"],
               [["docker", "create", "--add-host", "host.docker.internal:host-gateway", "ghcr.io/you/agent:latest"]])
    check_that("and torn down whatever happened", ["docker", "rm", "--force", CONTAINER] in docker.argvs, True)

    # --- each way to be non-compliant, named at the first failing assertion ---
    for label, docker, assertion in (
        ("no CMD", FakeDocker(config={"Cmd": [], "ExposedPorts": {}}), "the image has a CMD to run as PID 1"),
        ("an EXPOSE", FakeDocker(config={"Cmd": ["x"], "ExposedPorts": {"8080/tcp": {}}}), "the image declares no listening ports"),
        ("no identity call", FakeDocker(skip="identity"), "the agent calls GET /v1/agents/cloud/me with its token"),
        ("the wrong token", FakeDocker(skip="token"), "the agent presents the token from the credentials file"),
        ("no WebSocket", FakeDocker(skip="websocket"), "the agent opens the chat WebSocket"),
        ("running as root", FakeDocker(uids=("0",)), "the agent runs as uid 10000"),
        ("no reply", FakeDocker(skip="reply"), "the agent replies to one message"),
        ("a SIGKILL", FakeDocker(exit_code=137), "the agent exits cleanly on SIGTERM"),
    ):
        passed, failed = run_check(docker)
        check_that(f"check fails on {label}, naming that assertion", failed and failed.assertion, assertion)
        check_that(f"and {label} says what it saw instead", bool(failed and failed.saw), True)
        check_that(f"and {label} stops there, reporting nothing after it", assertion not in (passed or []), True)

    # --- the stub really is spoken to over a real socket ---------------------
    docker = FakeDocker()
    run_check(docker)
    check_that("the stub's one message reaches the agent over a real WebSocket", docker.heard, [PROMPT])

    # --- init copies the template -------------------------------------------
    with tempfile.TemporaryDirectory() as work:
        repo = os.path.join(work, "repo")
        written = template.copy_into(repo, slug="demo", image="ghcr.io/you/demo")
        shipped = sorted(os.path.relpath(path, repo) for path in written)
        check_that("init copies the whole template", shipped,
                   [".dockerignore", ".github/workflows/publish.yml", ".gitignore", "Dockerfile", "README.md",
                    "agent.py", "plow-agents.toml"])
        with open(os.path.join(repo, "plow-agents.toml")) as handle:
            toml = handle.read()
        check_that("and fills in the slug and image it was given",
                   ('slug = "demo"' in toml, 'image = "ghcr.io/you/demo"' in toml), (True, True))
        try:
            template.copy_into(repo)
            refused = False
        except SystemExit:
            refused = True
        check_that("and refuses to write over an existing repo", refused, True)

    # --- the reference agent owes nothing to Hermes --------------------------
    sources = {name: open(os.path.join(TEMPLATE, name)).read() for name in ("agent.py", "Dockerfile", "README.md")}
    for name, body in sources.items():
        found = sorted({word for word in ("hermes", "plow_chat", "plow-chat", "s6-overlay", "SOUL.md")
                        if word.lower() in body.lower()})
        check_that(f"the reference agent's {name} names nothing Hermes-shaped", found, [])
    imported = sorted({line.split()[1].split(".")[0] for line in sources["agent.py"].splitlines()
                       if line.startswith("import ") or line.startswith("from ")})
    check_that("and agent.py imports only the standard library and websockets", imported,
               ["__future__", "asyncio", "json", "logging", "os", "signal", "sys", "urllib", "websockets"])
    check_that("its Dockerfile is FROM a plain python base, not a Plow one",
               [line for line in sources["Dockerfile"].splitlines() if line.startswith("FROM ")], ["FROM python:3.13-slim"])
    check_that("and it declares no EXPOSE", "EXPOSE" in sources["Dockerfile"].replace("# No EXPOSE", ""), False)

    # --- the reference agent satisfies the contract it ships with ------------
    agent = {}
    exec(compile(sources["agent.py"], "agent.py", "exec"), agent)  # noqa: S102 -- our own file, read above
    check_that("the reference agent drops privileges before the network", "become_agent" in agent, True)
    check_that("and refuses a credential missing any of the three keys",
               _refuses(agent["read_credentials"]), True)

    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


def _refuses(read_credentials) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".creds", delete=False) as handle:
        handle.write("PLOW_API_BASE=http://x\n")
        path = handle.name
    try:
        read_credentials(path)
        return False
    except SystemExit:
        return True
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
