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
    def __init__(self, body, code=200, headers=None):
        super().__init__(json.dumps(body).encode())
        self.code = self.status = code
        self.headers = headers or {}


class Smoke(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.getcwd()
        os.chdir(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, self.previous)
        Path("token").write_text("synthetic-account")
        Path("plow-agents.toml").write_text(f'slug = "agent"\nimage = "{IMAGE}:v1"\n')
        self.commands = []
        self.requests = []
        self.fail_up = False
        self.fail_build = False
        self.private = False
        self.manifest = {"mediaType": "application/vnd.oci.image.manifest.v1+json"}
        self.realm = "https://ghcr.io/token"
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
        if "/manifests/" in url:
            if not req.get_header("Authorization"):
                return Response({}, 401, {"WWW-Authenticate": f'Bearer realm="{self.realm}",service="registry"'})
            self.assertEqual(req.get_header("Authorization"), "Bearer anonymous")
            return Response(self.manifest, 403 if self.private else 200)
        if "/token?" in url:
            self.assertIsNone(req.get_header("Authorization"))
            self.assertIn("scope=repository%3A", url)
            return Response({"token": "anonymous"})
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
            return Response({})
        if url.endswith("/agt_test"):
            return Response({"provider": "self_hosted"})
        if url.endswith("/v1/agents"):
            return Response([{"line": {"uid": "ln_free"}, "provider": f"exe:{IMAGE}@{DIGEST}", "status": "failed", "failure_code": "pull_failed"}])
        self.fail(f"unexpected request: {req.method} {url}")

    def run_cli(self, *args, success=True):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", [str(CLI), "--api-base", "https://api.example.test", "--token-file", "token", *args]):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = main()
                except SystemExit as error:
                    code = error.code
        self.assertEqual(code == 0, success, err.getvalue() or str(code))
        return out.getvalue(), err.getvalue()

    def test_image_build(self):
        self.run_cli("image", "build")
        self.assertEqual(self.commands, [["docker", "build", "--platform", "linux/amd64", "--tag", IMAGE + ":v1", "."]])
        Path("nested").mkdir()
        Path("nested/plow-credentials").write_text("secret")
        self.commands.clear()
        self.run_cli("image", "build", success=False)
        self.run_cli("deploy", "--local", success=False)
        self.assertFalse(self.commands)
        self.assertFalse(self.requests)

    def test_image_push(self):
        out, _ = self.run_cli("image", "push")
        self.assertEqual(out.strip(), f"{IMAGE}@{DIGEST}")
        self.assertEqual(tomllib.loads(Path("plow-agents.toml").read_text())["last_pushed"], DIGEST)
        self.assertEqual(self.commands, [["docker", "push", IMAGE + ":v1"]])
        self.assertIsNone(self.requests[0].get_header("Authorization"))
        self.private = True
        Path("plow-agents.toml").write_text(f'image = "{IMAGE}:v1"\n')
        self.run_cli("image", "push", success=False)
        self.assertNotIn("last_pushed", Path("plow-agents.toml").read_text())
        self.private = False
        self.manifest = {"manifests": []}
        Path("plow-agents.toml").write_text(f'image = "{IMAGE}:v1"\n')
        self.run_cli("image", "push", success=False)
        self.assertNotIn("last_pushed", Path("plow-agents.toml").read_text())
        for realm in ("http://ghcr.io/token", "https://other.example/token"):
            self.realm = realm
            self.requests.clear()
            self.run_cli("image", "push", success=False)
            self.assertEqual(len(self.requests), 1)
        self.realm = "https://auth.docker.io/token"
        self.manifest = {"mediaType": "application/vnd.oci.image.manifest.v1+json"}
        Path("plow-agents.toml").write_text('image = "docker.io/example/agent:v1"\n')
        self.requests.clear()
        self.run_cli("image", "push")
        self.assertIn("https://registry-1.docker.io/v2/example/agent/manifests/", self.requests[0].full_url)
        self.assertTrue(self.requests[1].full_url.startswith(self.realm))

    def test_deploy(self):
        for target in (f"{IMAGE}@{DIGEST}", DIGEST, "exe:hermes"):
            _, err = self.run_cli("deploy", target)
            self.assertIn("Requested", err)
            self.assertEqual(self.created[-1]["provider"], target if target.startswith("exe:") else f"exe:{IMAGE}@{DIGEST}")
            self.assertEqual(self.created[-1]["line_uid"], "ln_free")
        self.run_cli("image", "push")
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
        self.run_cli("deploy", "--local")
        self.assertEqual(self.commands, [["docker", "compose", "build"], ["docker", "compose", "up", "--no-build", "-d"]])
        self.assertEqual(self.created, [{"name": "plow-agent", "provider": "self_hosted", "line_uid": "ln_free"}])
        Path("plow-credentials").unlink()
        self.created.clear()
        self.fail_up = True
        self.run_cli("deploy", "--local", success=False)
        self.assertEqual(self.deleted, ["https://api.example.test/v1/agents/agt_test"])
        self.assertFalse(Path("plow-credentials").exists())

    def test_agents(self):
        out, _ = self.run_cli("agents")
        self.assertEqual(out, f"LINE\tTARGET\tSTATUS\nln_free\t{IMAGE}@{DIGEST}\tfailed(pull_failed)\n")


if __name__ == "__main__":
    unittest.main()
