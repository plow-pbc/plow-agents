#!/usr/bin/env python3
"""Exercise the CLI with fake Docker and HTTP; never touch a daemon or account."""
import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

CLI = Path(__file__).resolve().parents[1] / "bin/plow-agents"
main = runpy.run_path(str(CLI))["main"]
DIGEST = "sha256:" + "a" * 64
IMAGE = "ghcr.io/example/agent"


class Response(io.BytesIO):
    def __init__(self, body, code=200):
        super().__init__(json.dumps(body).encode())
        self.status = code


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
        Path("plow-agents.toml").write_text(f'slug = "agent"\nimage = "{IMAGE}:v1"\n')
        self.commands = []
        self.requests = []
        self.fail_revoke = False
        self.fail_up = False
        self.fail_build = False
        self.created = []
        self.deleted = []
        self.docker_patch = patch("subprocess.run", side_effect=self.docker)
        self.http_patch = patch("urllib.request.OpenerDirector.open", side_effect=self.http)
        self.docker_patch.start()
        self.http_patch.start()
        self.addCleanup(self.docker_patch.stop)
        self.addCleanup(self.http_patch.stop)

    def docker(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[1:3] == ["compose", "build"]:
            self.assertFalse(Path("plow-credentials").exists())
            self.assertFalse(self.created)
        if argv[1:3] == ["compose", "up"]:
            self.assertIn("PLOW_AGENT_TOKEN=synthetic-agent", Path("plow-credentials").read_text())
        return subprocess.CompletedProcess(argv, int((self.fail_up and "up" in argv) or (self.fail_build and "build" in argv)), f"v1: digest: {DIGEST} size: 123\n")

    def http(self, req, *args, **kwargs):
        self.requests.append(req)
        url = req.full_url
        self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-account")
        if url.endswith("/v1/chats"):
            return Response({"data": [{"status": "active", "participants": [{"type": "agent", "relationship": "self", "line": {"uid": "ln_free"}}]}]})
        if url.endswith("/v1/lines"):
            return Response({"data": [{"uid": "ln_free", "agent_uid": None}]})
        if req.method == "POST":
            self.created.append(json.loads(req.data))
            return Response({"agent": {"uid": "agt_test"}, "token": "synthetic-agent"})
        if req.method == "DELETE":
            self.deleted.append(url)
            return Response({}, 500 if self.fail_revoke else 200)
        if url.endswith("/agt_test"):
            return Response({"provider": "self_hosted"})
        if url.endswith("/v1/agents"):
            return Response([{"line": {"uid": "ln_free"}, "provider": f"exe:{IMAGE}@{DIGEST}", "status": "failed", "failure_code": "image_pull_timeout"}])
        self.fail(f"unexpected request: {req.method} {url}")

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
            self.run_cli("deploy", "--local", success=False)
            self.assertFalse(self.commands)
            self.assertFalse(self.requests)

    def test_account_token_in_build_context(self):
        Path("nested/plow").mkdir(parents=True)
        account_file = Path("nested/plow/token").resolve()
        account_file.write_text("synthetic-account")
        for override in (str(account_file), None):
            self.token_file = override
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path("nested").resolve())}):
                self.run_cli("image", "build", success=False)
                self.run_cli("deploy", "--local", success=False)
            self.assertFalse(self.commands)
            self.assertFalse(self.requests)

    def test_image_push(self):
        out, _ = self.run_cli("image", "push")
        self.assertEqual(out.strip(), f"{IMAGE}@{DIGEST}")
        self.assertEqual(tomllib.loads(Path("plow-agents.toml").read_text())["last_pushed"], f"{IMAGE}@{DIGEST}")
        self.assertEqual(self.commands, [["docker", "push", IMAGE + ":v1"]])
        self.assertFalse(self.requests)

    def test_push_preserves_edits_made_during_push(self):
        def pushing(argv, **kwargs):
            Path("plow-agents.toml").write_text('slug = "edited"\nimage = "ghcr.io/example/other:v2"\n')
            return self.docker(argv, **kwargs)
        with patch("subprocess.run", side_effect=pushing):
            self.run_cli("image", "push")
        self.assertEqual(tomllib.loads(Path("plow-agents.toml").read_text()), {
            "slug": "edited", "image": "ghcr.io/example/other:v2", "last_pushed": f"{IMAGE}@{DIGEST}",
        })

    def test_failed_push_record_preserves_project(self):
        config = Path("plow-agents.toml")
        original = config.read_bytes()
        with patch("os.replace", side_effect=OSError("replacement failed")):
            with self.assertRaisesRegex(OSError, "replacement failed"):
                self.run_cli("image", "push")
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in Path(".").iterdir()), ["plow-agents.toml"])

    def test_deploy(self):
        for target in (f"{IMAGE}@{DIGEST}", DIGEST, "exe:hermes"):
            _, err = self.run_cli("deploy", target)
            self.assertIn("Requested", err)
            self.assertEqual(self.created[-1]["provider"], target if target.startswith("exe:") else f"exe:{IMAGE}@{DIGEST}")
            self.assertEqual(self.created[-1]["line_uid"], "ln_free")
        self.run_cli("image", "push")
        config = Path("plow-agents.toml")
        config.write_text(config.read_text().replace(IMAGE + ":v1", "ghcr.io/example/other:v2"))
        self.run_cli("deploy", "--line", "ln_explicit")
        self.assertEqual(self.created[-1], {"name": "agent", "line_uid": "ln_explicit", "provider": f"exe:{IMAGE}@{DIGEST}"})
        self.created.clear()
        self.run_cli("deploy", IMAGE + ":v1", success=False)
        self.assertFalse(self.created)

    def test_deploy_local(self):
        self.fail_build = True
        self.run_cli("deploy", "--local", success=False)
        self.assertFalse(self.created)
        self.assertFalse(Path("plow-credentials").exists())
        self.fail_build = False
        self.commands.clear()
        self.run_cli("deploy", "--local", "--agent-api-base", "http://host.docker.internal:8000/v1")
        self.assertIn("PLOW_API_BASE=http://host.docker.internal:8000\n", Path("plow-credentials").read_text())
        self.assertEqual(self.commands, [["docker", "compose", "build"], ["docker", "compose", "up", "--no-build", "-d"]])
        self.assertEqual(self.created, [{"name": "plow-agent", "provider": "self_hosted", "line_uid": "ln_free"}])
        Path("plow-credentials").unlink()
        self.created.clear()
        self.fail_up = True
        self.run_cli("deploy", "--local", success=False)
        self.assertEqual(self.deleted, ["https://api.example.test/v1/agents/agt_test"])
        self.assertFalse(Path("plow-credentials").exists())

    def test_local_cleanup_failure_preserves_startup_error(self):
        self.fail_up = self.fail_revoke = True
        _, err = self.run_cli("deploy", "--local", success=False,
                              expected_error="plow-agents: docker compose up --no-build -d failed (1)")
        self.assertIn("agent may still be live", err)
        self.assertIn("HTTP 500", err)
        self.assertTrue(Path("plow-credentials").exists())
        self.assertEqual(self.deleted, ["https://api.example.test/v1/agents/agt_test"])

    def test_agents(self):
        out, _ = self.run_cli("agents")
        self.assertEqual(out, f"LINE\tTARGET\tSTATUS\nln_free\t{IMAGE}@{DIGEST}\tfailed(image_pull_timeout)\n")


if __name__ == "__main__":
    unittest.main()
