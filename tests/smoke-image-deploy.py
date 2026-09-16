#!/usr/bin/env python3
"""`image build|push`, `deploy` and `agents` against a fake docker and a stub API.

Standard library only, no network, no Plow account, and -- the point of the
injected runner -- no docker daemon: every `docker` argv the CLI would run is
recorded and answered from a script.

    python3 tests/smoke-image-deploy.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import httpx

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

from plow_agents import api, cli, config, images  # noqa: E402

IMAGE = "ghcr.io/plow-pbc/reference"
SHA = "sha256:" + "ab" * 32
FREE, HELD = "ln_free", "ln_held"


class Stub(BaseHTTPRequestHandler):
    created: list[dict] = []
    retired: list[str] = []
    lines = {"data": [{"uid": FREE, "display_name": "Free", "provider_key": "+15550001", "agent_uid": None},
                      {"uid": HELD, "display_name": "Held", "provider_key": "+15550002", "agent_uid": "agt_held"}]}
    chats = {"data": [{"status": "active", "participants": [{"type": "agent", "relationship": "self", "line": {"uid": uid}}]}
                      for uid in (FREE, HELD)]}
    agents = [
        {"uid": "agt_held", "provider": "exe:life", "line": {"uid": HELD}, "image": f"ghcr.io/plow-pbc/life@{SHA}",
         "status": "running", "failure_code": None},
        {"uid": "agt_direct", "provider": f"exe:{IMAGE}@{SHA}", "line": {"uid": FREE}, "image": f"{IMAGE}@{SHA}",
         "status": "failed", "failure_code": "image_pull_timeout"},
    ]

    def _send(self, status: int, payload: object) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        if self.path == "/v1/chats":
            return self._send(200, Stub.chats)
        if self.path == "/v1/lines":
            return self._send(200, Stub.lines)
        if self.path == "/v1/agents":
            return self._send(200, Stub.agents)
        if self.path.startswith("/v1/agents/"):
            return self._send(200, {"uid": self.path.rsplit("/", 1)[-1], "provider": "self_hosted"})
        self._send(404, {"detail": self.path})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/agents":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Stub.created.append(body)
            held = next((row for row in Stub.lines["data"] if row["uid"] == body["line_uid"]), {}).get("agent_uid")
            if held:
                return self._send(409, {"detail": {"code": "AGENT_EXISTS", "message": f"line already answers as {held}"}})
            agent = {"uid": "agt_new", "provider": body["provider"], "line": {"uid": body["line_uid"]}, "status": "provisioning"}
            return self._send(201, {"agent": agent, "token": "plow_minted" if body["provider"] == "self_hosted" else None})
        self._send(404, {"detail": self.path})

    def do_DELETE(self) -> None:  # noqa: N802
        Stub.retired.append(self.path.rsplit("/", 1)[-1])
        self._send(200, {})

    def log_message(self, *_: object) -> None:
        pass


class Registry:
    """ghcr's anonymous pull, as a MockTransport: a 401 challenge, a token endpoint, the manifest.

    `public=False` is a new ghcr package: the token endpoint refuses anyone
    without credentials. `manifest_status` overrides what the manifest answers
    to the anonymous token -- a registry that issues it and then refuses it.
    """

    def __init__(self, *, public: bool = True, manifest_status: int = 200, media: str = "application/vnd.oci.image.manifest.v1+json",
                 realm: str = "https://ghcr.io/token") -> None:
        self.public, self.manifest_status, self.media, self.realm = public, manifest_status, media, realm
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/token":
            if not self.public:
                return httpx.Response(403, json={"errors": [{"code": "DENIED"}]})
            return httpx.Response(200, json={"token": "anonymous"})
        if request.url.path == f"/v2/plow-pbc/reference/manifests/{SHA}" or request.url.path.startswith("/v2/plow-pbc/reference/manifests/"):
            if request.headers.get("Authorization") != "Bearer anonymous":
                return httpx.Response(401, headers={"WWW-Authenticate":
                    f'Bearer realm="{self.realm}",service="ghcr.io",scope="repository:plow-pbc/reference:pull"'})
            if self.manifest_status != 200:
                return httpx.Response(self.manifest_status)
            return httpx.Response(200, headers={"Content-Type": self.media}, content=b"{}")
        return httpx.Response(404)


class FakeDocker:
    """Every docker argv the CLI runs, and what it is told back."""

    def __init__(self, *, digest: str = SHA, fail: str | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.digest, self.fail = digest, fail

    def __call__(self, argv: list[str], env: dict[str, str]) -> tuple[int, str]:
        self.calls.append((argv, env))
        if self.fail is not None and self.fail in argv:
            return 1, ""
        if argv[1] == "push":
            return 0, f"abc123: Pushed\nlatest: digest: {self.digest} size: 528\n"
        return 0, ""

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


def run(*argv: str, cwd: str, base: str, token: str, docker: FakeDocker | None = None,
        registry: Registry | None = None) -> tuple[int, str, str]:
    """Drive the real Typer app in-process, with the docker seam replaced, and the registry when given."""
    out, err = io.StringIO(), io.StringIO()
    full = ["plow-agents", "--api-base", base, "--token-file", token, *argv]
    original = os.getcwd()
    os.chdir(cwd)
    try:
        transport = httpx.MockTransport(registry) if registry is not None else api.TRANSPORT
        with patch.object(sys, "argv", full), patch.object(cli, "subprocess_runner", docker or FakeDocker()), \
                patch.object(api, "TRANSPORT", transport):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    cli.app()
                    code = 0
                except SystemExit as error:
                    code = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
                    if isinstance(error.code, str):
                        print(error.code, file=err)
    finally:
        os.chdir(original)
    return code, out.getvalue(), err.getvalue()


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    failures = []

    def check(what: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{what}\n  want: {want!r}\n  got:  {got!r}")
        print(f"{'ok  ' if got == want else 'FAIL'} {what}")

    with tempfile.TemporaryDirectory() as work:
        token = os.path.join(work, "token")
        with open(token, "w") as handle:
            handle.write("acct_stub\n")
        toml = os.path.join(work, config.CONFIG_FILE)

        # --- plow-agents.toml: the unit of identity -------------------------
        with open(toml, "w") as handle:
            handle.write(f'slug = "reference"\nimage = "{IMAGE}"\n')
        check("toml read gives slug and image", (config.load(work).slug, config.load(work).image), ("reference", IMAGE))
        check("toml has no digest before a push", config.load(work).last_pushed, "")
        check("--image overrides the toml without writing it",
              (config.load(work, image="ghcr.io/other/x").image, config.load(work).image), ("ghcr.io/other/x", IMAGE))

        missing = os.path.join(work, "elsewhere")
        os.makedirs(missing)
        code, _, err = run("image", "push", cwd=missing, base=base, token=token)
        check("a verb with no toml names the field it wanted", (code != 0, "no image in" in err), (True, True))

        # --- image build ----------------------------------------------------
        docker = FakeDocker()
        code, _, _ = run("image", "build", cwd=work, base=base, token=token, docker=docker)
        check("build exits 0", code, 0)
        check("build is linux/amd64, tagged from the toml", docker.argvs,
              [["docker", "build", "--platform", "linux/amd64", "--tag", f"{IMAGE}:latest", "."]])

        docker = FakeDocker(fail="build")
        code, _, err = run("image", "build", cwd=work, base=base, token=token, docker=docker)
        check("a failed build is fatal", (code != 0, "build failed" in err), (True, True))

        # A live credential anywhere under the build directory is a token
        # `COPY . .` would bake in, however deep it was minted.
        for label, where in (("holding", ""), ("with a credential minted in a child of", "child/deeper")):
            credentialled = os.path.join(work, f"with-credential-{len(where)}")
            os.makedirs(os.path.join(credentialled, where) if where else credentialled)
            # A buildable directory, so the guard is the only thing that can stop it.
            with open(os.path.join(credentialled, config.CONFIG_FILE), "w") as handle:
                handle.write(f'image = "{IMAGE}"\n')
            with open(os.path.join(credentialled, where, "plow-credentials"), "w") as handle:
                handle.write("PLOW_AGENT_TOKEN=plow_live\n")
            docker = FakeDocker()
            code, _, err = run("image", "build", cwd=credentialled, base=base, token=token, docker=docker)
            check(f"build refuses a directory {label} it, naming the file, and docker never sees it",
                  (code != 0, os.path.join(where, "plow-credentials") in err, docker.argvs), (True, True, []))

        for written, want in (("ghcr.io/you/agent", ("ghcr.io", "you/agent")),
                              ("docker.io/you/agent", ("registry-1.docker.io", "you/agent")),
                              ("docker.io/python", ("registry-1.docker.io", "library/python")),
                              ("you/agent", ("registry-1.docker.io", "you/agent")),
                              ("localhost:5000/agent", ("localhost:5000", "agent"))):
            check(f"{written} is pulled from {want[0]} as {want[1]}", images.registry_of(written), want)

        # --- image push -----------------------------------------------------
        docker, registry = FakeDocker(), Registry()
        code, out, _ = run("image", "push", cwd=work, base=base, token=token, docker=docker, registry=registry)
        check("push exits 0", code, 0)
        check("push pushes the tag, and runs no other docker command", docker.argvs, [["docker", "push", f"{IMAGE}:latest"]])
        check("push asks the registry for the pushed digest, is challenged, takes the anonymous token, and asks again",
              [(request.method, request.url.path) for request in registry.requests],
              [("GET", f"/v2/plow-pbc/reference/manifests/{SHA}"), ("GET", "/token"), ("GET", f"/v2/plow-pbc/reference/manifests/{SHA}")])
        check("and the token request carries the challenge's scope and no credentials",
              (dict(registry.requests[1].url.params), "Authorization" in registry.requests[1].headers),
              ({"service": "ghcr.io", "scope": "repository:plow-pbc/reference:pull"}, False))
        check("push prints the digest-pinned reference", out.strip(), f"{IMAGE}@{SHA}")
        check("push records last_pushed in the toml", config.load(work).last_pushed, SHA)
        with open(toml) as handle:
            check("and leaves the other keys alone", 'slug = "reference"' in handle.read(), True)

        other = "sha256:" + "ef" * 32
        for label, registry, words in (
            ("a private package (the token endpoint refuses)", Registry(public=False), "is not public"),
            ("a manifest refused to the anonymous token", Registry(manifest_status=403), "is not public"),
            ("a manifest the registry cannot find", Registry(manifest_status=404), "HTTP 404"),
            ("a multi-architecture index", Registry(media="application/vnd.oci.image.index.v1+json"), "multi-architecture"),
        ):
            code, _, err = run("image", "push", cwd=work, base=base, token=token, docker=FakeDocker(digest=other), registry=registry)
            check(f"push fails on {label}, saying so", (code != 0, words in err), (True, True))
            check(f"and {label} leaves last_pushed as it was", config.load(work).last_pushed, SHA)

        # The realm is the registry's to name; a token is only fetched from where it should come from.
        for label, realm in (("an http realm", "http://ghcr.io/token"), ("a realm on another host", "https://attacker.example/token")):
            registry = Registry(realm=realm)
            code, _, err = run("image", "push", cwd=work, base=base, token=token, docker=FakeDocker(digest=other), registry=registry)
            check(f"push refuses {label}, naming it, and never asks it for a token",
                  (code != 0, realm in err, [r.url.path for r in registry.requests if r.url.path == "/token"]), (True, True, []))
        code, out, _ = run("image", "push", "--image", "docker.io/plow-pbc/reference", cwd=work, base=base, token=token,
                           docker=FakeDocker(digest=other), registry=Registry(realm="https://auth.docker.io/token"))
        check("Docker Hub's token host is the one other realm followed", (code, out.strip()), (0, f"docker.io/plow-pbc/reference@{other}"))
        for registry_host, realm in (("registry.example:5000", "https://registry.example:5000/token"),
                                     ("registry.example:443", "https://registry.example/token")):
            code, out, _ = run("image", "push", "--image", f"{registry_host}/plow-pbc/reference", cwd=work, base=base, token=token,
                               docker=FakeDocker(digest=other), registry=Registry(realm=realm))
            check(f"{registry_host} takes a realm on the same host and port: {realm}",
                  (code, out.strip()), (0, f"{registry_host}/plow-pbc/reference@{other}"))

        docker = FakeDocker(digest="not-a-digest")
        code, _, err = run("image", "push", cwd=work, base=base, token=token, docker=docker, registry=Registry())
        check("a push that prints no sha256 digest is fatal", (code != 0, "no sha256 digest" in err), (True, True))
        check("and last_pushed is left as it was", config.load(work).last_pushed, SHA)

        second = "sha256:" + "cd" * 32
        run("image", "push", cwd=work, base=base, token=token, docker=FakeDocker(digest=second), registry=Registry())
        check("a second push replaces last_pushed rather than appending", config.load(work).last_pushed, second)
        with open(toml) as handle:
            check("leaving exactly one last_pushed line", handle.read().count("last_pushed"), 1)
        # TOML ignores leading whitespace, so an indented key is the same key.
        indented = os.path.join(work, "indented", config.CONFIG_FILE)
        os.makedirs(os.path.dirname(indented))
        with open(indented, "w") as handle:
            handle.write(f'slug = "x"\nimage = "{IMAGE}"\n  last_pushed = "{SHA}"\n')
        config.record_last_pushed(indented, second)
        with open(indented) as handle:
            written = handle.read()
        check("an indented last_pushed is replaced, not duplicated", written.count("last_pushed"), 1)
        check("and the indentation is kept", '  last_pushed = ' in written, True)
        check("and the new digest is what the file now reads", config.load(os.path.dirname(indented)).last_pushed, second)
        config.record_last_pushed(toml, SHA)

        # A checkout can ship its toml as a symlink; push must not write through it.
        linked = os.path.join(work, "linked")
        os.makedirs(linked)
        target = os.path.join(work, "target.toml")
        with open(target, "w") as handle:
            handle.write(f'image = "{IMAGE}"\n')
        os.symlink(target, os.path.join(linked, config.CONFIG_FILE))
        code, _, err = run("image", "push", cwd=linked, base=base, token=token, docker=FakeDocker(digest=second), registry=Registry())
        with open(target) as handle:
            check("push refuses a symlinked toml and leaves its target alone",
                  (code != 0, "symlink" in err, handle.read()), (True, True, f'image = "{IMAGE}"\n'))

        overridden = os.path.join(work, "overridden")
        os.makedirs(overridden)
        with open(os.path.join(overridden, config.CONFIG_FILE), "w") as handle:
            handle.write('image = "ghcr.io/you/else"\n')
        code, out, _ = run("image", "push", "--image", IMAGE, cwd=overridden, base=base, token=token,
                           docker=FakeDocker(digest=second), registry=Registry())
        check("push --image prints the overriding reference but records nothing against the toml's image",
              (code, out.strip(), config.load(overridden).last_pushed), (0, f"{IMAGE}@{second}", ""))

        # --- deploy ---------------------------------------------------------
        Stub.created.clear()
        code, out, err = run("deploy", cwd=work, base=base, token=token)
        check("deploy exits 0", code, 0)
        check("deploy sends the digest-pinned image as the provider", Stub.created[-1],
              {"name": "reference", "line_uid": FREE, "provider": f"exe:{IMAGE}@{SHA}"})
        check("deploy prints the line and the reference it asked for", out.strip().split("\t"), ["agt_new", FREE, f"{IMAGE}@{SHA}"])
        check("deploy says requested, not deployed, and names the phase", ("Requested agent agt_new" in err, "provisioning" in err), (True, True))
        check("deploy does not claim the agent is up", "Deployed" in err, False)

        Stub.created.clear()
        explicit = "sha256:" + "ef" * 32
        run("deploy", explicit, "--line", FREE, cwd=work, base=base, token=token)
        check("a digest argument beats last_pushed", Stub.created[-1]["provider"], f"exe:{IMAGE}@{explicit}")

        Stub.created.clear()
        code, _, _ = run("deploy", "--image", "ghcr.io/other/x", cwd=work, base=base, token=token)
        check("deploy takes no --image, so an image and a digest cannot come from different places",
              (code != 0, Stub.created), (True, []))

        Stub.created.clear()
        code, _, err = run("deploy", "latest", cwd=work, base=base, token=token)
        check("deploy refuses a tag", (code != 0, "not a sha256 digest" in err, Stub.created), (True, True, []))

        code, _, err = run("deploy", "--line", HELD, cwd=work, base=base, token=token)
        check("an occupied line is Plow's refusal, reported with what Plow said",
              (code != 0, "already answers as agt_held" in err), (True, True))

        held = dict(Stub.lines)
        Stub.lines = {"data": [dict(row, agent_uid="agt_x") for row in Stub.lines["data"]]}
        Stub.created.clear()
        code, _, err = run("deploy", cwd=work, base=base, token=token)
        check("deploy with no free line says so before creating anything",
              (code != 0, "no free line" in err, Stub.created), (True, True, []))
        Stub.lines = {"data": [dict(row, agent_uid=None) for row in held["data"]]}
        code, _, err = run("deploy", cwd=work, base=base, token=token)
        check("deploy with two free lines asks which", (code != 0, "more than one free line" in err), (True, True))
        Stub.lines = held

        empty = os.path.join(work, "no-toml")
        os.makedirs(empty)
        code, _, err = run("deploy", SHA, cwd=empty, base=base, token=token)
        check("deploy with no image configured names the field", (code != 0, "no image in" in err), (True, True))

        # --- deploy's targets: a listing, or any image by digest --------------
        Stub.created.clear()
        code, out, _ = run("deploy", "exe:life", "--line", FREE, cwd=work, base=base, token=token)
        check("deploy exe:life asks Plow for the listing by slug", (code, Stub.created[-1]),
              (0, {"name": "life", "line_uid": FREE, "provider": "exe:life"}))
        Stub.created.clear()
        run("deploy", f"ghcr.io/other/agent@{SHA}", "--line", FREE, cwd=empty, base=base, token=token)
        check("deploy image@sha256 needs no toml and sends that image", Stub.created[-1]["provider"], f"exe:ghcr.io/other/agent@{SHA}")
        Stub.created.clear()
        code, _, err = run("deploy", "ghcr.io/other/agent:latest", cwd=work, base=base, token=token)
        check("deploy refuses an image by tag", (code != 0, Stub.created), (True, []))

        # --- deploy --local: mint, then compose up here -----------------------
        local = os.path.join(work, "local")
        os.makedirs(local)
        code, _, err = run("deploy", "--local", "--line", FREE, cwd=local, base=base, token=token)
        check("deploy --local with no compose.yml says so and mints nothing",
              (code != 0, "no compose.yml" in err, os.path.exists(os.path.join(local, "plow-credentials"))), (True, True, False))
        with open(os.path.join(local, "compose.yml"), "w") as handle:
            handle.write("services: {}\n")
        docker = FakeDocker()
        Stub.created.clear()
        code, out, _ = run("deploy", "--local", "--line", FREE, cwd=local, base=base, token=token, docker=docker)
        check("deploy --local builds, then mints a self-hosted credential",
              (code, Stub.created[-1]["provider"], os.path.isfile(os.path.join(local, "plow-credentials"))), (0, "self_hosted", True))
        check("in that order: the build comes before the mint, and the run does not build again",
              docker.argvs, [["docker", "compose", "build"], ["docker", "compose", "up", "--no-build", "-d"]])
        check("and ends on the command to follow its logs", out.strip().splitlines()[-1], "docker compose logs -f")

        docker = FakeDocker()
        Stub.created.clear()
        code, _, err = run("deploy", "--local", "--line", FREE, cwd=local, base=base, token=token, docker=docker)
        check("a second --local refuses the credential already here, before anything is built",
              (code != 0, "already exists" in err, docker.argvs, Stub.created), (True, True, [], []))

        failing = os.path.join(work, "failing-build")
        os.makedirs(failing)
        with open(os.path.join(failing, "compose.yml"), "w") as handle:
            handle.write("services: {}\n")
        docker = FakeDocker(fail="build")
        Stub.created.clear()
        code, _, err = run("deploy", "--local", "--line", FREE, cwd=failing, base=base, token=token, docker=docker)
        check("a build that fails mints nothing and leaves no credential",
              (code != 0, "build failed" in err, Stub.created, os.path.exists(os.path.join(failing, "plow-credentials"))),
              (True, True, [], False))
        check("and it never reached compose up", docker.argvs, [["docker", "compose", "build"]])

        failing_up = os.path.join(work, "failing-up")
        os.makedirs(failing_up)
        with open(os.path.join(failing_up, "compose.yml"), "w") as handle:
            handle.write("services: {}\n")
        docker = FakeDocker(fail="up")
        Stub.created.clear()
        Stub.retired.clear()
        code, _, err = run("deploy", "--local", "--line", FREE, cwd=failing_up, base=base, token=token, docker=docker)
        check("a compose up that fails retires the agent it just minted, and takes the credential with it",
              (code != 0, Stub.created and Stub.created[-1]["provider"], Stub.retired,
               os.path.exists(os.path.join(failing_up, "plow-credentials"))),
              (True, "self_hosted", ["agt_new"], False))

        # --- agents ---------------------------------------------------------
        code, out, _ = run("agents", cwd=work, base=base, token=token)
        check("agents exits 0", code, 0)
        rows = {row.split("\t")[0]: row.split("\t") for row in out.splitlines()[1:]}
        check("agents heads its columns", out.splitlines()[0], "LINE\tSLUG\tSTATUS\tIMAGE")
        check("agents shows a listing deploy by slug, running", rows.get(HELD), [HELD, "life", "running", f"ghcr.io/plow-pbc/life@{SHA}"])
        # A digest is 71 characters. A table sized to the terminal ellipsised
        # it, which is the one field of this verb nobody can retype.
        check("agents shows a direct image deploy by its whole digest, and why it failed",
              rows.get(FREE), [FREE, "-", "failed (image_pull_timeout)", f"{IMAGE}@{SHA}"])

    server.shutdown()
    for failure in failures:
        print(f"\n{failure}", file=sys.stderr)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
