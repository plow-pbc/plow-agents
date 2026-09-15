#!/usr/bin/env python3
"""`image check` and `init`, against a fake docker and a real stub server.

No docker daemon and no network beyond loopback. The fake runner answers the
`docker` argv the CLI would run. On `docker start` it runs the **real template
`agent.py`** as a process of its own, reading the credential the CLI actually
wrote and talking to the real stub over a real WebSocket; `docker kill` sends
that process a real SIGTERM. The one seam is privilege: a test cannot become
root and drop to uid 10000, so `setgroups`/`setgid`/`setuid` are recorded
rather than performed.

The ways to fail the contract are a smaller scripted agent in a thread, since
the reference agent cannot be made to break it; `top`, `exec` and `inspect`
answers are scripted too.

    python3 tests/smoke-image-check.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

from websockets.sync.client import connect  # noqa: E402

from plow_agents import check, template  # noqa: E402

TEMPLATE = os.path.join(SRC, "plow_agents", "template")
CONTAINER = "c0ffee"
INIT_PID = "4242"
COMPLIANT = {"Cmd": ["python3", "/opt/agent/agent.py"]}
CONTRACT = ["the image has a CMD to run as PID 1", check.TOKEN_ASSERTION]

# Stands in for the kernel on the three calls a non-root test process cannot
# make, and prints each so the order of the drop can be read back.
PRIVILEGE_SEAM = """
import os, runpy, sys
ids = {"uid": 0}
os.getuid = os.geteuid = lambda: ids["uid"]
os.setgroups = lambda groups: print(f"seam: setgroups {groups}", flush=True)
os.setgid = lambda gid: print(f"seam: setgid {gid}", flush=True)
def setuid(uid):
    print(f"seam: setuid {uid}", flush=True)
    ids["uid"] = uid
os.setuid = setuid
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def call(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"null")


def fake_agent(base: str, token: str, *, skip: str | None, stopping: threading.Event) -> None:
    """A compliant agent with one step removable: the contract's to fail the check, the advice's to warn."""
    try:
        if skip == "anything":
            return
        if skip != "me":
            call("GET", f"{base}/v1/agents/cloud/me", "wrong-token" if skip == "token" else token)
        if skip in ("token", "websocket"):
            return
        ticket = call("POST", f"{base}/v1/ws/ticket", token, {})["ticket"]
        with connect(f"{base.replace('http', 'ws', 1)}/v1/ws?ticket={ticket}") as socket:
            while not stopping.is_set():
                frame = json.loads(socket.recv(timeout=10))
                if frame.get("event_type") != "message_received" or skip == "reply":
                    continue
                call("POST", f"{base}/v1/chats/{frame['chat_id']}/messages", token, {"body": "ack"})
                stopping.wait()
    except Exception as error:  # noqa: BLE001 -- a fake agent that dies is a failing check
        print(f"    (fake agent stopped: {type(error).__name__}: {error})")


class FakeDocker:
    """Every docker argv `check` runs: the agent is real or scripted, the daemon never is."""

    def __init__(self, *, config: dict | None = None, real: bool = False, skip: str | None = None,
                 processes: tuple[tuple[str, str], ...] = ((INIT_PID, "10000"),),
                 kill_status: int = 0,
                 ignores_term: bool = False, exits: int = 0, exited_early: bool = False, busybox: bool = False) -> None:
        self.argvs: list[list[str]] = []
        # A daemon whose `ps` has no `uid` column, only `user` (OrbStack's).
        self.busybox = busybox
        self.config = COMPLIANT if config is None else config
        self.real, self.skip, self.processes = real, skip, processes
        self.kill_status = kill_status
        self.ignores_term, self.exits, self.exited_early = ignores_term, exits, exited_early
        self.stopping = threading.Event()
        self.work = tempfile.mkdtemp(prefix="image-check-")
        self.log = os.path.join(self.work, "agent.log")
        self.process: subprocess.Popen | None = None
        # Kept rather than pointed at: the CLI stages the credential in a
        # temporary directory it deletes when the check returns.
        self.credentials = ""
        self.credential_mode = 0
        self.cp: list[str] = []

    def _start(self) -> None:
        # What `--add-host host.docker.internal:host-gateway` does for a
        # container, done to the file: this process reaches the stub on loopback.
        rewritten = self.credentials.replace("host.docker.internal", "127.0.0.1")
        values = dict(line.split("=", 1) for line in rewritten.splitlines() if "=" in line)
        if not self.real:
            threading.Thread(target=fake_agent, args=(values["PLOW_API_BASE"], values["PLOW_AGENT_TOKEN"]),
                             kwargs={"skip": self.skip, "stopping": self.stopping}, daemon=True).start()
            return
        credentials = os.path.join(self.work, "credentials")
        with open(credentials, "w") as handle:
            handle.write(rewritten)
        os.chmod(credentials, 0o600)
        with open(self.log, "w") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-c", PRIVILEGE_SEAM, os.path.join(TEMPLATE, "agent.py")],
                env={**os.environ, "PLOW_CREDENTIALS": credentials}, stdout=log, stderr=subprocess.STDOUT)

    def _state(self) -> str:
        if self.exited_early:
            return "exited 1"
        if self.process is not None:
            code = self.process.poll()
            # A signal death reads as docker reports it: 128 + the signal.
            return "running 0" if code is None else f"exited {128 - code if code < 0 else code}"
        return f"exited {self.exits}" if self.stopping.is_set() else "running 0"

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
            self._start()
            return 0, ""
        if argv[1] == "top":
            if self.busybox and argv[4] == "pid,uid,args":
                return 1, ""
            column = "USER" if self.busybox else "UID"
            return 0, f"PID    {column}    COMMAND\n" + "".join(
                f"{pid}   {'root' if self.busybox and uid == '0' else uid}   python3 agent.py\n" for pid, uid in self.processes)
        if argv[1] == "inspect":
            return 0, (INIT_PID if argv[3] == "{{.State.Pid}}" else self._state()) + "\n"
        if argv[1] == "kill":
            if self.kill_status == 0 and not self.ignores_term:
                if self.process is not None:
                    self.process.send_signal(signal.SIGTERM)
                self.stopping.set()
            return self.kill_status, ""
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
                passed, warned = check.check(docker, image="ghcr.io/you/plow-agents:latest", timeout=10, advice_wait=3, stop_timeout=3)
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
    check_that("with no warning", (warned, "warn" in output), ([], False))
    agent_log = docker.agent_log()
    check_that("the agent drops groups, then gid, then uid, before it says anything",
               [line for line in agent_log.splitlines() if line.startswith(("seam:", "INFO starting"))][:4],
               ["seam: setgroups []", "seam: setgid 10000", "seam: setuid 10000", "INFO starting as uid 10000"])
    check_that("it reads the stub's schema-shaped frame and answers that chat", "INFO replied in cht_check" in agent_log, True)
    check_that("and the SIGTERM really ended its process, with 0", docker.process and docker.process.returncode, 0)
    check_that("the credential is a direct deploy's: no AGENT_ID, which the agent must not need",
               sorted(line.split("=")[0] for line in docker.credentials.splitlines()), ["PLOW_AGENT_TOKEN", "PLOW_API_BASE"])
    check_that("and mode 600, as Plow writes it", oct(docker.credential_mode), "0o600")
    check_that("and names the container by the host it can reach this machine on",
               "PLOW_API_BASE=http://host.docker.internal:" in docker.credentials, True)
    check_that("it is copied in as root rather than bind-mounted, so the mode is Plow's",
               (docker.cp[1], docker.cp[3]), ("cp", f"{CONTAINER}:/var/lib/"))
    check_that("the container is created with no command override",
               [argv for argv in docker.argvs if argv[1] == "create"],
               [["docker", "create", "--add-host", "host.docker.internal:host-gateway", "ghcr.io/you/plow-agents:latest"]])
    check_that("the stop is a SIGTERM the check times itself, not `docker stop`",
               ([argv for argv in docker.argvs if argv[1] == "kill"], any(argv[1] == "stop" for argv in docker.argvs)),
               ([["docker", "kill", "--signal", "TERM", CONTAINER]], False))
    check_that("and torn down whatever happened", ["docker", "rm", "--force", CONTAINER] in docker.argvs, True)

    # --- only the contract fails the check ----------------------------------
    for label, docker, assertion, saw in (
        ("no CMD", FakeDocker(config={"Cmd": []}), CONTRACT[0], "neither Cmd nor Entrypoint is set"),
        ("an agent that calls nothing", FakeDocker(skip="anything"), CONTRACT[1], "no request reached the API"),
        ("the wrong token", FakeDocker(skip="token"), CONTRACT[1], "requests arrived without it"),
    ):
        passed, warned, failed, _ = run_check(docker)
        check_that(f"check fails on {label}, naming that assertion", failed and failed.assertion, assertion)
        check_that(f"and {label} says what it saw instead", saw in (failed.saw if failed else ""), True)

    # --- the advice warns, and never fails ----------------------------------
    identity, websocket, reply = "it calls GET /v1/agents/cloud/me on boot", "it opens the chat WebSocket", "it replies to a message"
    uid, sigterm = "every process but PID 1 runs as uid 10000", "it exits cleanly on SIGTERM"
    for label, docker, warnings in (
        ("a root PID 1 whose agent is uid 10000", FakeDocker(processes=((INIT_PID, "0"), ("4300", "10000"))), []),
        ("a busybox ps, root PID 1 and a uid-10000 agent", FakeDocker(busybox=True, processes=((INIT_PID, "0"), ("4300", "10000"))), []),
        ("no identity call", FakeDocker(skip="me"), [identity]),
        ("no WebSocket", FakeDocker(skip="websocket"), [websocket, reply]),
        ("no reply", FakeDocker(skip="reply"), [reply]),
        ("running as root", FakeDocker(processes=((INIT_PID, "0"),)), [uid]),
        ("a busybox ps showing a root agent", FakeDocker(busybox=True, processes=((INIT_PID, "0"), ("4300", "0"))), [uid]),
        ("a root agent with a uid-10000 child", FakeDocker(processes=((INIT_PID, "0"), ("4300", "0"), ("4301", "10000"))), [uid]),
        ("an agent that ignores SIGTERM", FakeDocker(ignores_term=True), [sigterm]),
        ("a nonzero exit on SIGTERM", FakeDocker(exits=137), [sigterm]),
        ("a container already gone", FakeDocker(exited_early=True), [sigterm]),
        ("a failed docker kill", FakeDocker(kill_status=1), [sigterm]),
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
               ["__future__", "asyncio", "collections", "functools", "json", "logging", "os", "signal", "sys", "threading",
                "urllib", "websockets"])
    check_that("its Dockerfile is FROM a plain python base, not a Plow one",
               [line for line in sources["Dockerfile"].splitlines() if line.startswith("FROM ")], ["FROM python:3.13-slim"])
    check_that("and it declares no EXPOSE", "EXPOSE" in sources["Dockerfile"].replace("# No EXPOSE", ""), False)

    # --- the reference agent satisfies the contract it ships with ------------
    agent = {}
    exec(compile(sources["agent.py"], "agent.py", "exec"), agent)  # noqa: S102 -- our own file, read above
    check_that("the reference agent refuses a credential missing any of the three keys",
               _refuses(agent["read_credentials"]), True)

    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


class Plow:
    """A Plow that identifies the agent and then either hangs or refuses the ticket."""

    def __init__(self, ticket: str) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        plow = self
        self.ticket_asked, self.tickets = threading.Event(), 0

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
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
        self.work = tempfile.mkdtemp(prefix="image-check-plow-")
        self.agent: subprocess.Popen | None = None

    def __enter__(self) -> Plow:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_: object) -> None:
        if self.agent is not None and self.agent.poll() is None:
            self.agent.kill()
            self.agent.wait()
        self.server.shutdown()

    def boot(self) -> subprocess.Popen:
        credentials = os.path.join(self.work, "credentials")
        with open(credentials, "w") as handle:
            handle.write(f"AGENT_ID=demo\nPLOW_API_BASE=http://127.0.0.1:{self.server.server_address[1]}\nPLOW_AGENT_TOKEN=t\n")
        self.agent = subprocess.Popen(
            [sys.executable, "-c", PRIVILEGE_SEAM, os.path.join(TEMPLATE, "agent.py")],
            env={**os.environ, "PLOW_CREDENTIALS": credentials}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.agent


def _exit_within(process: subprocess.Popen, seconds: float) -> int | None:
    try:
        return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return None


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
