#!/usr/bin/env python3
"""`image check` and `init`, against a fake docker and a real stub server.

No docker daemon and no network beyond loopback. The fake runner answers the
`docker` argv the CLI would run. On `docker run` it runs the **real template
`agent.py`** as a process of its own, with the environment the CLI actually
passed, talking to the real stub over a real WebSocket.

The ways to fail the contract are a smaller scripted agent in a thread, since
the reference agent cannot be made to break it.

    python3 tests/smoke-image-check.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

from websockets.sync.client import connect  # noqa: E402

from plow_agents import check, stub, template  # noqa: E402

TEMPLATE = os.path.join(SRC, "plow_agents", "template")
CONTAINER = "c0ffee"
COMPLIANT = {"Cmd": ["python3", "/opt/agent/agent.py"]}
CONTRACT = ["the image has a CMD to run as PID 1", check.CALL_ASSERTION]

def call(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"null")


def fake_agent(base: str, *, skip: str | None, stopping: threading.Event) -> None:
    """A compliant agent with one step removable: the contract's to fail the check, the advice's to warn."""
    try:
        if skip == "anything":
            return
        if skip != "me":
            call("GET", f"{base}/v1/agents/cloud/me")
        if skip == "websocket":
            return
        ticket = call("POST", f"{base}/v1/ws/ticket", {})["ticket"]
        with connect(f"{base.replace('http', 'ws', 1)}/v1/ws?ticket={ticket}") as socket:
            while not stopping.is_set():
                frame = json.loads(socket.recv(timeout=10))
                if frame.get("event_type") != "message_received" or skip == "reply":
                    continue
                call("POST", f"{base}/v1/chats/{frame['chat_id']}/messages", {"body": "ack"})
                stopping.wait()
    except Exception as error:  # noqa: BLE001 -- a fake agent that dies is a failing check
        print(f"    (fake agent stopped: {type(error).__name__}: {error})")


class FakeDocker:
    """Every docker argv `check` runs: the agent is real or scripted, the daemon never is."""

    def __init__(self, *, config: dict | None = None, real: bool = False, skip: str | None = None) -> None:
        self.argvs: list[list[str]] = []
        self.config = COMPLIANT if config is None else config
        self.real, self.skip = real, skip
        self.stopping = threading.Event()
        self.log = os.path.join(tempfile.mkdtemp(prefix="image-check-"), "agent.log")
        self.process: subprocess.Popen | None = None
        self.environment: dict[str, str] = {}

    def _run(self, argv: list[str]) -> None:
        self.environment = dict(argv[index + 1].split("=", 1) for index, part in enumerate(argv) if part == "--env")
        # What `--add-host host.docker.internal:host-gateway` does for a
        # container, done to the value: this process reaches the stub on loopback.
        base = self.environment["PLOW_API_BASE"].replace("host.docker.internal", "127.0.0.1")
        if not self.real:
            threading.Thread(target=fake_agent, args=(base,),
                             kwargs={"skip": self.skip, "stopping": self.stopping}, daemon=True).start()
            return
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("PLOW_", "AGENT_ID"))}
        with open(self.log, "w") as log:
            self.process = subprocess.Popen(
                [sys.executable, os.path.join(TEMPLATE, "agent.py")],
                env={**clean, **self.environment, "PLOW_API_BASE": base}, stdout=log, stderr=subprocess.STDOUT)

    def __call__(self, argv: list[str], env: dict[str, str]) -> tuple[int, str]:
        self.argvs.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return 0, json.dumps(self.config)
        if argv[1] == "run":
            self._run(argv)
            return 0, CONTAINER + "\n"
        if argv[1] == "rm":
            self.stopping.set()
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait()
        return 0, ""

    def agent_log(self) -> str:
        with open(self.log) as handle:
            return handle.read()


def main() -> int:
    failures = []

    def check_that(what: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{what}\n  want: {want!r}\n  got:  {got!r}")
        print(f"{'ok  ' if got == want else 'FAIL'} {what}")

    def run_check(docker: FakeDocker) -> tuple[list[str] | None, list[str] | None, check.ContractError | None, str]:
        """(passed, warned, failure, what it printed)."""
        output = io.StringIO()
        try:
            with contextlib.redirect_stderr(output):
                passed, warned = check.check(docker, image="ghcr.io/you/plow-agents:latest", timeout=10, advice_wait=3)
            return passed, warned, None, output.getvalue()
        except check.ContractError as failure:
            return None, None, failure, output.getvalue()

    # --- the reference agent passes, with no warnings ------------------------
    docker = FakeDocker(real=True)
    passed, warned, failed, output = run_check(docker)
    check_that("check passes on the real template agent.py", failed and f"{failed.assertion}: {failed.saw}", None)
    if failed:
        print(docker.agent_log())
    check_that("and makes exactly the contract's two assertions", passed, CONTRACT)
    check_that("with no warning, having replied to the owner in quoted bytes",
               (warned, "warn" in output, "it said: 'Hi Owner" in output), ([], False, True))
    check_that("the container gets what a VM gets: PLOW_API_BASE alone, no token and no AGENT_ID",
               sorted(docker.environment), ["PLOW_API_BASE"])
    check_that("and names the stub by the host a container reaches this machine on",
               docker.environment.get("PLOW_API_BASE", "").startswith("http://host.docker.internal:"), True)
    check_that("under a per-run path only the container is told",
               bool(re.fullmatch(r"http://host\.docker\.internal:\d+/chk_[0-9a-f]{32}",
                                 docker.environment.get("PLOW_API_BASE", ""))), True)
    check_that("it is started with no command override",
               [argv[:5] + argv[-1:] for argv in docker.argvs if argv[1] == "run"],
               [["docker", "run", "--detach", "--add-host", "host.docker.internal:host-gateway", "ghcr.io/you/plow-agents:latest"]])
    check_that("and torn down whatever happened", ["docker", "rm", "--force", CONTAINER] in docker.argvs, True)

    # --- only the contract fails the check ----------------------------------
    for label, docker, assertion, saw in (
        ("no CMD", FakeDocker(config={"Cmd": []}), CONTRACT[0], "neither Cmd nor Entrypoint is set"),
        ("an agent that calls nothing", FakeDocker(skip="anything"), CONTRACT[1], "no request reached the API"),
    ):
        passed, warned, failed, _ = run_check(docker)
        check_that(f"check fails on {label}, naming that assertion", failed and failed.assertion, assertion)
        check_that(f"and {label} says what it saw instead", saw in (failed.saw if failed else ""), True)

    # --- the advice warns, and never fails ----------------------------------
    identity, websocket, reply = "it calls GET /v1/agents/cloud/me on boot", "it opens the chat WebSocket", "it replies to a message"
    for label, docker, warnings in (
        ("no identity call", FakeDocker(skip="me"), [identity]),
        ("no WebSocket", FakeDocker(skip="websocket"), [websocket, reply]),
        ("no reply", FakeDocker(skip="reply"), [reply]),
    ):
        passed, warned, failed, output = run_check(docker)
        check_that(f"{label} passes the contract", (failed and failed.assertion, passed), (None, CONTRACT))
        check_that(f"and {label} warns about exactly that", warned, warnings)
        check_that("and prints it as a warn line", all(f"warn {advice}" in output for advice in warnings), True)

    # --- the reference agent stops when told, and gives up on an answer -----
    with Plow("hang") as plow:
        agent = plow.boot()
        asked = plow.ticket_asked.wait(10)
        agent.send_signal(signal.SIGTERM)
        check_that("a SIGTERM during a request that never answers still stops the agent, cleanly",
                   (asked, _exit_within(agent, 5)), (True, 0))
    with Plow("refuse") as plow:
        agent = plow.boot()
        code = _exit_within(agent, 10)
        check_that("a 401 on the ticket is raised, not retried forever", code not in (None, 0), True)
        check_that("and it asked once", plow.tickets, 1)
    for token, want in ((None, None), ("t", "Bearer t")):
        with Plow("hang") as plow:
            plow.boot(token=token)
            plow.ticket_asked.wait(10)
            check_that(f"PLOW_AGENT_TOKEN={token!r} sends Authorization {want!r}", plow.authorizations[:1], [want])

    # --- init copies the template -------------------------------------------
    with tempfile.TemporaryDirectory() as work:
        repo = os.path.join(work, "repo")
        written = template.copy_into(repo, slug="plow-agents", image="ghcr.io/you/plow-agents")
        shipped = sorted(os.path.relpath(path, repo) for path in written)
        check_that("init copies the whole template", shipped,
                   [".dockerignore", ".github/workflows/publish.yml", ".gitignore", "Dockerfile", "README.md",
                    "agent.py", "plow-agents.toml"])
        with open(os.path.join(repo, "plow-agents.toml")) as handle:
            toml = handle.read()
        check_that("and fills in the slug and image it was given",
                   ('slug = "plow-agents"' in toml, 'image = "ghcr.io/you/plow-agents"' in toml), (True, True))
        try:
            template.copy_into(repo)
            refused = ""
        except FileExistsError as error:
            refused = error.filename or ""
        check_that("and refuses to write over an existing repo, naming the file",
                   os.path.basename(refused) in shipped, True)

    # A collision half way through leaves the checkout as it was found.
    with tempfile.TemporaryDirectory() as work:
        repo = os.path.join(work, "repo")
        os.makedirs(repo)
        with open(os.path.join(repo, "README.md"), "w") as handle:
            handle.write("mine")
        try:
            template.copy_into(repo)
            refused = ""
        except FileExistsError as error:
            refused = os.path.basename(error.filename or "")
        check_that("init refuses an existing README.md", refused, "README.md")
        check_that("and removes the files it had already written", sorted(os.listdir(repo)), ["README.md"])
        check_that("leaving theirs untouched", open(os.path.join(repo, "README.md")).read(), "mine")

    # A checkout's symlinks -- a linked parent, a dangling link where a file goes -- are not written through.
    with tempfile.TemporaryDirectory() as work:
        outside = os.path.join(work, "outside")
        os.makedirs(outside)
        for label, link, points_at in (("a symlinked parent", ".github", outside),
                                       ("a dangling symlink", "Dockerfile", os.path.join(outside, "Dockerfile"))):
            repo = os.path.join(work, f"repo-{link}")
            os.makedirs(repo)
            os.symlink(points_at, os.path.join(repo, link))
            try:
                template.copy_into(repo)
                refused = False
            except SystemExit:
                refused = True
            check_that(f"init refuses {label}, and writes nothing outside the repo", (refused, os.listdir(outside)), (True, []))

    # --- the reference agent owes nothing to Hermes --------------------------
    sources = {name: open(os.path.join(TEMPLATE, name)).read() for name in ("agent.py", "Dockerfile", "README.md")}
    for name, body in sources.items():
        found = sorted({word for word in ("hermes", "plow_chat", "plow-chat", "s6-overlay", "SOUL.md")
                        if word.lower() in body.lower()})
        check_that(f"the reference agent's {name} names nothing Hermes-shaped", found, [])
    imported = sorted({line.split()[1].split(".")[0] for line in sources["agent.py"].splitlines()
                       if line.startswith("import ") or line.startswith("from ")})
    check_that("and agent.py imports only the standard library and websockets", imported,
               ["__future__", "asyncio", "collections", "functools", "json", "logging", "os", "signal", "sys", "threading",
                "urllib", "websockets"])
    check_that("its Dockerfile is FROM a plain python base, not a Plow one",
               [line for line in sources["Dockerfile"].splitlines() if line.startswith("FROM ")], ["FROM python:3.13-slim"])
    check_that("and it declares no EXPOSE", "EXPOSE" in sources["Dockerfile"].replace("# No EXPOSE", ""), False)

    # --- the reference agent satisfies the contract it ships with ------------
    agent = {}
    exec(compile(sources["agent.py"], "agent.py", "exec"), agent)  # noqa: S102 -- our own file, read above
    check_that("the reference agent refuses to start without PLOW_API_BASE", _refuses(agent["read_environment"]), True)
    check_that("and needs nothing else", agent["read_environment"]({"PLOW_API_BASE": "http://x/"}), ("http://x", None))

    # --- a peer that does not know the realm cannot satisfy the assertion ---
    with stub.Stub(host="127.0.0.1") as local:
        base = f"http://127.0.0.1:{local.port}"
        for label, url in (("the bare identity route", f"{base}/v1/agents/cloud/me"),
                           ("a guessed realm", f"{base}/chk_{'0' * 32}/v1/agents/cloud/me")):
            check_that(f"a tokenless peer calling {label} is 404ed", _status(url), 404)
        check_that("and none of it counts as the image calling the API", local.seen.token.is_set(), False)
        check_that("while the realm it handed the container does", _status(f"{base}{local.realm}/v1/agents/cloud/me"), 200)
        check_that("and that one counts", local.seen.token.is_set(), True)

    # --- the stub reads no body before the route and the token say whose ----
    with stub.Stub(host="127.0.0.1", token="plow_x") as local:
        for label, path, token, want in (("off-realm", "/v1/ws/ticket", local.token, 404),
                                         ("unrouted", f"{local.realm}/v1/elsewhere", "", 404),
                                         ("unauthenticated", f"{local.realm}/v1/chats/{stub.CHAT_UID}/messages", "", 401),
                                         ("authenticated but over 64 KiB", f"{local.realm}/v1/ws/ticket", local.token, 413)):
            check_that(f"an {label} POST claiming a terabyte body is answered at once, and closed",
                       _huge_post(local.port, path, token), (want, True))

    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


class Plow:
    """A Plow that identifies the agent and then either hangs or refuses the ticket."""

    def __init__(self, ticket: str) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        plow = self
        self.ticket_asked, self.tickets, self.authorizations = threading.Event(), 0, []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                plow.authorizations.append(self.headers.get("Authorization"))
                self._answer(200, {"line": {"uid": "ln_x"}, "chats": []})

            def do_POST(self) -> None:  # noqa: N802
                plow.tickets += 1
                plow.ticket_asked.set()
                if ticket == "hang":
                    threading.Event().wait(60)
                self._answer(401, {"detail": "no"})

            def _answer(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.agent: subprocess.Popen | None = None

    def __enter__(self) -> Plow:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_: object) -> None:
        if self.agent is not None and self.agent.poll() is None:
            self.agent.kill()
            self.agent.wait()
        self.server.shutdown()

    def boot(self, *, token: str | None = "t") -> subprocess.Popen:
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("PLOW_", "AGENT_ID"))}
        environment = {"AGENT_ID": "demo", "PLOW_API_BASE": f"http://127.0.0.1:{self.server.server_address[1]}",
                       **({"PLOW_AGENT_TOKEN": token} if token else {})}
        self.agent = subprocess.Popen([sys.executable, os.path.join(TEMPLATE, "agent.py")], env={**clean, **environment},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.agent


def _huge_post(port: int, path: str, token: str) -> tuple[int | None, bool]:
    """POST claiming a terabyte body and sending none: (status, whether the stub then closed)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        auth = f"Authorization: Bearer {token}\r\n" if token else ""
        conn.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\n{auth}Content-Length: {10**12}\r\n\r\n".encode())
        received = b""
        try:
            while chunk := conn.recv(4096):
                received += chunk
        except TimeoutError:
            return None, False
    status = received.split(b" ", 2)[1] if received.startswith(b"HTTP/") else None
    return (int(status) if status else None), True


def _status(url: str) -> int | None:
    """The status of a plain GET, whatever it is."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _exit_within(process: subprocess.Popen, seconds: float) -> int | None:
    try:
        return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return None


def _refuses(read_environment) -> bool:
    try:
        read_environment({"PLOW_AGENT_TOKEN": "t"})
        return False
    except SystemExit:
        return True


if __name__ == "__main__":
    sys.exit(main())
