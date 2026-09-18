#!/usr/bin/env python3
"""Exercise the CLI with fake Docker and HTTP; never touch a daemon or account."""
import contextlib
import email.message
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CLI = Path(__file__).resolve().parents[1] / "bin/plow-agents"
main = runpy.run_path(str(CLI))["main"]
DIGEST = "sha256:" + "a" * 64
IMAGE = "ghcr.io/example/agent"
TAG_DIGEST = "sha256:" + "b" * 64
MANIFEST_URL = "https://ghcr.io/v2/example/agent/manifests/v1"
TOKEN_URL = "https://ghcr.io/token"
CHALLENGE = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:example/agent:pull"'


class Response(io.BytesIO):
    def __init__(self, body, code=200):
        super().__init__(json.dumps(body).encode())
        self.status = code


class Headers(io.BytesIO):
    """A registry answer: the headers are the payload, the body is empty.

    `email.message.Message` is what urllib itself puts on `.headers`, and it
    looks names up case-insensitively -- which a plain dict does not, and which
    is the whole reason `Docker-Content-Digest` can be read off a real answer.
    """

    def __init__(self, headers, code=200):
        super().__init__(b"")
        self.status = code
        self.headers = email.message.Message()
        for name, value in headers.items():
            self.headers[name] = value


class Smoke(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.getcwd()
        work = Path(self.directory.name) / "work"
        work.mkdir()
        os.chdir(work)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, self.previous)
        self.token_file = str(Path(self.directory.name) / "token")
        Path(self.token_file).write_text("synthetic-account")
        Path("plow-agents.toml").write_text(f'image = "{IMAGE}:v1"\n')
        self.commands = []
        self.requests = []
        self.fail_up = False
        self.created = []
        self.deleted = []
        self.registry = []
        self.tag_status = 200
        self.tag_digest = TAG_DIGEST
        self.anonymous_token = "synthetic-registry"
        self.docker_patch = patch("subprocess.run", side_effect=self.docker)
        self.http_patch = patch("urllib.request.OpenerDirector.open", side_effect=self.http)
        self.docker_patch.start()
        self.http_patch.start()
        self.addCleanup(self.docker_patch.stop)
        self.addCleanup(self.http_patch.stop)

    def docker(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[1:3] == ["compose", "up"]:
            self.assertIn("PLOW_AGENT_TOKEN=synthetic-agent", Path("plow-credentials").read_text())
        return subprocess.CompletedProcess(argv, int(self.fail_up), f"v1: digest: {DIGEST} size: 123\n", stderr="compose startup failed: fixture error\n" if self.fail_up else "")

    def http(self, req, *args, **kwargs):
        url = req.full_url
        if url.startswith(("https://ghcr.io/", "https://public.ecr.aws/")):
            return self.registry_http(req)
        self.requests.append(req)
        self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-account")
        if url.endswith("/v1/lines"):
            # `provider_type` on every row, because the API sends it on every
            # row and both `mint` and a remote `deploy` now read it.
            return Response({"data": [
                {"uid": "ln_free", "agent_uid": None, "provider_type": "imessage"},
                {"uid": "ln_explicit", "agent_uid": None, "provider_type": "imessage"},
                {"uid": "ln_mail", "agent_uid": None, "provider_type": "email"},
            ]})
        if req.method == "POST":
            self.created.append(json.loads(req.data))
            return Response({"agent": {"uid": "agt_test"}, "token": "synthetic-agent"})
        if req.method == "DELETE":
            self.deleted.append(url)
            return Response({})
        if url.endswith("/agt_test"):
            return Response({"provider": "self_hosted"})
        if url.endswith("/v1/agents"):
            return Response([{"line": {"uid": "ln_free"}, "provider": f"exe:{IMAGE}@{DIGEST}", "status": "failed", "failure_code": "image_pull_timeout"}])
        self.fail(f"unexpected request: {req.method} {url}")

    def registry_http(self, req):
        """The manifest HEAD and its token endpoint.

        The account token must never arrive here, so that is asserted on every
        registry request rather than in one test: a registry is not Plow, and
        the bug this guards against is one nobody would see in the output.
        """
        self.registry.append((req.method, req.full_url, req.get_header("Authorization")))
        self.assertNotIn("synthetic-account", req.get_header("Authorization") or "")
        if req.full_url.startswith(TOKEN_URL):
            return Response({"token": self.anonymous_token})
        self.assertEqual(req.method, "HEAD")
        accept = req.get_header("Accept")
        for kind in ("oci.image.index.v1", "docker.distribution.manifest.list.v2"):
            self.assertIn(kind, accept)
        if req.get_header("Authorization") is None:
            return Headers({"WWW-Authenticate": CHALLENGE}, 401)
        if self.tag_status != 200:
            return Headers({}, self.tag_status)
        return Headers({"Docker-Content-Digest": self.tag_digest} if self.tag_digest else {})

    def run_cli(self, *args, success=True, expected_error=None):
        out, err = io.StringIO(), io.StringIO()
        token_args = ["--token-file", self.token_file] if self.token_file else []
        with patch.object(sys, "argv", [str(CLI), "--api-base", "https://api.example.test", *token_args, *args]):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = main()
                except SystemExit as error:
                    code = error.code
        self.assertEqual(code == 0, success, err.getvalue() or str(code))
        if expected_error is not None:
            self.assertEqual(code, expected_error)
        return out.getvalue(), err.getvalue()

    def test_image_build(self):
        self.run_cli("image", "build")
        self.assertEqual(self.commands, [["docker", "build", "--platform", "linux/amd64", "--tag", IMAGE + ":v1", "."]])
        Path("nested").mkdir()
        for credential in ("# plow-agent-uid: agt_test\n", "PLOW_AGENT_TOKEN=synthetic-agent\n"):
            Path("nested/custom.env").write_text(credential)
            self.commands.clear()
            self.run_cli("image", "build", success=False)
            self.assertFalse(self.commands)
            self.assertFalse(self.requests)

    def test_build_honors_dockerignore(self):
        credential = Path("plow-credentials")
        credential.write_text("PLOW_AGENT_TOKEN=synthetic-agent\n")
        cases = [
            (None, False), ("# plow-credentials\n", False),
            ("/plow-credentials\n", True), ("./plow-credentials/\n", True),
            ("plow-*\n", True), ("**/plow-credentials\n", True),
            ("plow-credential?\n", True), ("plow-credential[s]\n", True),
            ("plow-*\n!plow-credentials\n", False),
            ("*\n!plow-*\nplow-credentials\n", True),
        ]
        for rules, excluded in cases:
            with self.subTest(rules=rules):
                if rules is not None:
                    Path(".dockerignore").write_text(rules)
                self.commands.clear()
                self.run_cli("image", "build", success=excluded, expected_error=None if excluded else
                             "plow-agents: add plow-credentials to .dockerignore before building")
                self.assertEqual(bool(self.commands), excluded)
                self.assertEqual(credential.read_text(), "PLOW_AGENT_TOKEN=synthetic-agent\n")
        Path("Dockerfile.dockerignore").write_text("")
        self.commands.clear()
        self.run_cli("image", "build", success=False,
                     expected_error="plow-agents: add plow-credentials to Dockerfile.dockerignore before building")
        self.assertFalse(self.commands)
        Path("Dockerfile.dockerignore").write_text("plow-credentials\n")
        self.run_cli("image", "build")

    def test_build_scans_ignored_dockerfile(self):
        Path(".dockerignore").write_text("Dockerfile\n")
        Path("Dockerfile").write_text("FROM scratch\n# plow-agent-uid: agt_test\n")
        self.run_cli("image", "build", success=False,
                     expected_error="plow-agents: remove credentials from Dockerfile before building")
        self.assertFalse(self.commands)
        self.assertFalse(self.requests)

    def test_build_refuses_dockerfile_env_credential(self):
        Path("Dockerfile").write_text("FROM scratch\nENV PLOW_AGENT_TOKEN=synthetic-agent\n")
        self.run_cli("image", "build", success=False,
                     expected_error="plow-agents: remove credentials from Dockerfile before building")
        self.assertFalse(self.commands)

    def test_build_ignores_nested_credentials_and_account_token(self):
        Path("nested").mkdir()
        Path("nested/custom.env").write_text("# plow-agent-uid: agt_test\n")
        self.token_file = str(Path("nested/token").resolve())
        Path(self.token_file).write_text("synthetic-account")
        Path(".dockerignore").write_text("nested/\n")
        self.run_cli("image", "build")
        self.commands.clear()
        Path(".dockerignore").write_text("nested/\n!nested/token\n")
        self.run_cli("image", "build", success=False,
                     expected_error="plow-agents: add nested/token to .dockerignore before building")
        self.assertFalse(self.commands)

    def test_account_token_in_build_context(self):
        Path("nested/plow").mkdir(parents=True)
        account_file = Path("nested/plow/token").resolve()
        account_file.write_text("synthetic-account")
        for override in (str(account_file), None):
            self.token_file = override
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path("nested").resolve())}):
                self.run_cli("image", "build", success=False)
            self.assertFalse(self.commands)
            self.assertFalse(self.requests)

    def test_image_arguments_and_read_only_config(self):
        config = Path("plow-agents.toml")
        for contents in (None, f'image = "{IMAGE}:v1"\n', 'not valid toml'):
            for verb in ("build", "push"):
                with self.subTest(contents=contents, verb=verb):
                    if contents is None:
                        config.unlink(missing_ok=True)
                    else:
                        config.write_text(contents)
                    self.commands.clear()
                    with patch("os.replace", side_effect=AssertionError("must not write")):
                        out, _ = self.run_cli("image", verb, IMAGE + ":v2")
                    self.assertIn(IMAGE + ":v2", self.commands[-1])
                    if verb == "push":
                        self.assertEqual(out.splitlines()[-1], f"{IMAGE}@{DIGEST}")
                    self.assertEqual(config.read_text() if config.exists() else None, contents)
                    self.assertEqual(list(Path(".").iterdir()), [config] if config.exists() else [])
        config.write_text(f'image = "{IMAGE}:v1"\n')
        original = config.read_bytes()
        out, _ = self.run_cli("image", "push")
        self.assertEqual(out.splitlines()[-1], f"{IMAGE}@{DIGEST}")
        self.assertEqual(config.read_bytes(), original)
        config.unlink()
        for verb in ("build", "push"):
            self.run_cli("image", verb, success=False,
                         expected_error="plow-agents: pass IMAGE or set image in ./plow-agents.toml")

    def test_image_registry_host(self):
        for host in ("localhost", "localhost:5000", "registry:5000", "registry:5.0"):
            with self.subTest(host=host):
                Path("plow-agents.toml").write_text(f'image = "{host}/agent:v1"\n')
                self.run_cli("image", "build", success=False)
                self.assertFalse(self.commands)
        self.run_cli("image", "build", "registry.example:5000/agent:v1")
        self.assertIn("registry.example:5000/agent:v1", self.commands[-1])

    def test_deploy(self):
        Path("plow-agents.toml").write_text('not valid toml')
        for target, name in ((f"{IMAGE}@{DIGEST}", "agent"), ("exe:hermes", "hermes")):
            _, err = self.run_cli("deploy", target, "--line", "ln_explicit")
            self.assertIn("Requested", err)
            self.assertEqual(self.created[-1], {
                "name": name, "line_uid": "ln_explicit",
                "provider": target if target.startswith("exe:") else f"exe:{IMAGE}@{DIGEST}",
            })
        # A remote deploy validates the line before it creates anything: an
        # unknown uid and a mailbox are both refused by name, with no POST.
        self.created.clear()
        for line_uid, said in (
            ("ln_nope", "unknown line:ln_nope"),
            ("ln_mail", "line:ln_mail is not a phone line -- `plow-agents lines` lists the ones an agent can answer on"),
        ):
            self.run_cli("deploy", "exe:hermes", "--line", line_uid, success=False, expected_error=f"plow-agents: {said}")
        self.assertFalse(self.created)
        self.created.clear()
        self.requests.clear()
        Path("plow-agents.toml").write_text(f'image = "{IMAGE}:v1"\nlast_pushed = "{IMAGE}@{DIGEST}"\n')
        # A tag in the config is still not a target: `deploy` names what it
        # deploys. A bare digest and a bare repository name neither.
        for target in ((), (DIGEST,), (IMAGE,)):
            with self.subTest(target=target):
                self.run_cli("deploy", *target, "--line", "ln_free", success=False,
                             expected_error="plow-agents: deploy needs image@sha256:<64 hex>, "
                                            "image:tag, exe:<slug>, or --local")
                self.assertFalse(self.requests)
                self.assertFalse(self.registry)

    def test_deploy_resolves_a_tag_to_a_digest(self):
        out, err = self.run_cli("deploy", IMAGE + ":v1", "--line", "ln_explicit")
        # What Plow is asked for is the digest, never the tag: a name its owner
        # can move must not be what the VM pulls.
        self.assertEqual(self.created[-1], {
            "name": "agent", "line_uid": "ln_explicit", "provider": f"exe:{IMAGE}@{TAG_DIGEST}"})
        self.assertIn(f"resolved {IMAGE}:v1 -> {TAG_DIGEST}", err)
        self.assertEqual(out, "")
        # Anonymous first, then once more with the token that 401 named -- and
        # the second HEAD is the one that carries it.
        self.assertEqual([(method, url) for method, url, _ in self.registry], [
            ("HEAD", MANIFEST_URL),
            ("GET", TOKEN_URL + "?service=ghcr.io&scope=repository%3Aexample%2Fagent%3Apull"),
            ("HEAD", MANIFEST_URL),
        ])
        self.assertEqual([auth for _, _, auth in self.registry],
                         [None, None, "Bearer synthetic-registry"])

    def test_deploy_passes_a_digest_through_untouched(self):
        self.run_cli("deploy", f"{IMAGE}@{DIGEST}", "--line", "ln_explicit")
        self.assertEqual(self.created[-1]["provider"], f"exe:{IMAGE}@{DIGEST}")
        # No registry was asked anything: a digest is already the answer.
        self.assertFalse(self.registry)

    def test_a_tag_that_does_not_resolve_deploys_nothing(self):
        for status, said in ((404, "the registry answered 404"), (500, "the registry answered 500")):
            with self.subTest(status=status):
                self.tag_status = status
                self.created.clear()
                self.run_cli("deploy", IMAGE + ":v1", "--line", "ln_explicit", success=False,
                             expected_error=f"plow-agents: {IMAGE}:v1 did not resolve: {said}")
                self.assertFalse(self.created)
        # A 200 carrying no digest is a registry too old for this, not a
        # missing tag, and says so rather than reporting the tag absent.
        self.tag_status, self.tag_digest = 200, ""
        self.created.clear()
        self.run_cli("deploy", IMAGE + ":v1", "--line", "ln_explicit", success=False,
                     expected_error=f"plow-agents: {IMAGE}:v1 resolved, but the registry sent "
                                    "no Docker-Content-Digest")
        self.assertFalse(self.created)

    def test_a_registry_port_is_not_a_tag(self):
        """`registry:5000/agent` has a colon that is a port, not a tag."""
        cli = runpy.run_path(str(CLI))
        for image, tag in (
            ("registry.example:5000/agent", None),
            ("registry.example:5000/agent:v1", "v1"),
            ("ghcr.io/example/agent", None),
            ("ghcr.io/example/agent:v1", "v1"),
            ("ghcr.io/example/agent:1.2.3-rc.1", "1.2.3-rc.1"),
        ):
            with self.subTest(image=image):
                self.assertEqual(cli["image_tag"](image), tag)

    def test_local_requires_credential_exclusion_before_mint(self):
        for rules in (None, "plow-*\n!plow-credentials\n"):
            with self.subTest(rules=rules):
                if rules is not None:
                    Path(".dockerignore").write_text(rules)
                self.run_cli("deploy", "--local", "--line", "ln_free", success=False,
                             expected_error="plow-agents: add plow-credentials to .dockerignore before building")
                self.assertFalse(self.requests)
                self.assertFalse(self.commands)
                self.assertFalse(Path("plow-credentials").exists())

    def test_deploy_requires_an_explicit_line(self):
        Path(".dockerignore").write_text("plow-credentials\n")
        for target in ("exe:hermes", "--local"):
            with self.subTest(target=target):
                _, err = self.run_cli("deploy", target, success=False, expected_error=2)
                self.assertIn("required: --line", err)
                self.assertFalse(self.requests)
                self.assertFalse(self.commands)
                self.assertFalse(Path("plow-credentials").exists())

    def test_deploy_local(self):
        Path(".dockerignore").write_text("plow-credentials\n")
        fixture = Path("tests/__pycache__/test_plow_init.cpython-311.pyc")
        fixture.parent.mkdir(parents=True)
        fixture.write_bytes(b"\x00\nPLOW_AGENT_TOKEN=synthetic-fixture\n")
        self.run_cli("deploy", "--local", "--line", "ln_free", "--agent-api-base", "http://host.docker.internal:8000/v1")
        self.assertIn("PLOW_API_BASE=http://host.docker.internal:8000\n", Path("plow-credentials").read_text())
        self.assertEqual(self.commands, [["docker", "compose", "up", "--build", "-d"]])
        self.assertEqual(self.created, [{"name": "plow-agent", "provider": "self_hosted", "line_uid": "ln_free"}])

    def test_docker_failure_prints_stderr(self):
        self.fail_up = True
        for verb in ("build", "push"):
            _, err = self.run_cli("image", verb, success=False)
            self.assertIn("compose startup failed: fixture error", err)

    def test_local_startup_failure_preserves_agent_and_credential(self):
        Path(".dockerignore").write_text("plow-credentials\n")
        self.fail_up = True
        _, err = self.run_cli("deploy", "--local", "--line", "ln_free", success=False,
                              expected_error="plow-agents: docker compose up --build -d failed (1)")
        self.assertIn("compose startup failed: fixture error", err)
        self.assertFalse(self.deleted)
        self.assertEqual(self.created, [{"name": "plow-agent", "provider": "self_hosted", "line_uid": "ln_free"}])
        self.assertEqual(Path("plow-credentials").read_text(),
                         "PLOW_API_BASE=https://api.example.test\nPLOW_AGENT_TOKEN=synthetic-agent\n# plow-agent-uid: agt_test\n")
        self.assertIn("Agent and plow-credentials kept", err)
        self.assertIn("plow-agents revoke", err)
        self.assertIn("docker compose up --build -d", err)

    def test_agents(self):
        out, _ = self.run_cli("agents")
        self.assertEqual(out, f"LINE\tTARGET\tSTATUS\nln_free\t{IMAGE}@{DIGEST}\tfailed(image_pull_timeout)\n")


if __name__ == "__main__":
    unittest.main()
