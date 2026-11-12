#!/usr/bin/env python3
"""`image build` and `image push` with fake Docker; any HTTP request fails the test."""
import contextlib
import io
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
        self.fail_docker = False
        self.docker_patch = patch("subprocess.run", side_effect=self.docker)
        self.http_patch = patch("urllib.request.OpenerDirector.open", side_effect=self.http)
        self.docker_patch.start()
        self.http_patch.start()
        self.addCleanup(self.docker_patch.stop)
        self.addCleanup(self.http_patch.stop)

    def docker(self, argv, **kwargs):
        self.commands.append(argv)
        return subprocess.CompletedProcess(argv, int(self.fail_docker), f"v1: digest: {DIGEST} size: 123\n", stderr="docker failed: fixture error\n" if self.fail_docker else "")

    def http(self, req, *args, **kwargs):
        self.requests.append(req)
        self.fail(f"unexpected request: {req.method} {req.full_url}")

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

    def test_docker_failure_prints_stderr(self):
        self.fail_docker = True
        for verb in ("build", "push"):
            _, err = self.run_cli("image", verb, success=False)
            self.assertIn("docker failed: fixture error", err)


if __name__ == "__main__":
    unittest.main()
