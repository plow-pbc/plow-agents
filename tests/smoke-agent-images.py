#!/usr/bin/env python3
"""Run image commands with fake HTTP and Docker; no accounts or registry access."""
import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

CLI = Path(__file__).resolve().parents[1] / "bin/plow-agents"
main = runpy.run_path(str(CLI))["main"]
REF = "ghcr.io/example/agent@sha256:" + "a" * 64


class Response(io.BytesIO):
    def __init__(self, body, code=200):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = code


class Smoke(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = Path(self.directory.name) / "token"
        self.token.write_text("synthetic-account")
        self.row = {"slug": "hermes", "name": "Hermes",
                    "image": REF, "enabled": True, "phrases": ["Hello"], "updated_at": "2026-09-18T00:00:00Z"}
        self.index = {"agent_id": "hermes", "name": "Site name", "image": "old", "installs": {"total": 7}}
        self.requests = []
        self.commands = []
        self.plow_status = 200
        self.index_status = 200
        self.dropped = {}
        self.http_patch = patch("urllib.request.OpenerDirector.open", side_effect=self.http)
        self.docker_patch = patch("subprocess.run", side_effect=self.docker)
        self.http_patch.start()
        self.docker_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.addCleanup(self.docker_patch.stop)

    def http(self, req, *args, **kwargs):
        self.requests.append(req)
        body = json.loads(req.data) if req.data else None
        if "/v1/agent-images/" in req.full_url:
            if req.method == "GET":
                self.assertIsNone(req.get_header("Authorization"))
                return Response(self.row or {"detail": "missing"}, 200 if self.row else 404)
            self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-account")
            if self.plow_status != 200:
                return Response({"detail": "permission denied"}, self.plow_status)
            if self.row is None:
                self.row = {"slug": urllib.parse.unquote(req.full_url.rsplit("/", 1)[1]), "image": None}
            self.row.update(body)
            return Response(self.row)
        if req.full_url.endswith("/v1/auth/index-identity"):
            self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-account")
            return Response({"assertion": "synthetic-index-assertion"})
        if "/v1/agent?" in req.full_url:
            self.assertIsNone(req.get_header("Authorization"))
            return Response(self.index or {"ok": False, "error": "no such agent"}, 200 if self.index else 404)
        if "/v1/agents?" in req.full_url:
            slug = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)["agent_id"][0]
            self.assertEqual(slug, self.row["slug"] if self.row else "hermes")
            self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-index-assertion")
            if self.index_status != 200:
                return Response({"error": "index refused"}, self.index_status)
            if not self.dropped:
                if self.index is None:
                    self.index = {"agent_id": slug}
                self.index.update(body)
            return Response({"ok": True, "result": "updated", "dropped": self.dropped})
        self.fail(f"unexpected request: {req.method} {req.full_url}")

    def docker(self, argv, **kwargs):
        self.commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "v1: digest: sha256:" + "a" * 64 + " size: 123\n", "")

    def run_cli(self, *args, success=True, token=True, index_base="https://index.example.test"):
        out, err = io.StringIO(), io.StringIO()
        argv = [str(CLI), "--api-base", "https://api.example.test"]
        if index_base:
            argv += ["--index-base", index_base]
        argv += ["--token-file", str(self.token if token else self.token.with_name("missing")), *args]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main()
            except SystemExit as error:
                code = error.code
                if isinstance(code, str):
                    print(code, file=sys.stderr)
        self.assertEqual(code == 0, success, err.getvalue())
        self.assertNotIn("synthetic-account", out.getvalue() + err.getvalue())
        self.assertNotIn("synthetic-index-assertion", out.getvalue() + err.getvalue())
        return out.getvalue(), err.getvalue()

    def writes(self):
        return [r for r in self.requests if r.method in ("POST", "PUT")]

    def test_promote_and_clear(self):
        for extra, plow_image, index_image in [([REF], REF, REF), (["--none"], None, "")]:
            with self.subTest(extra=extra):
                self.requests.clear()
                out, err = self.run_cli("image", "promote", "hermes", *extra)
                self.assertEqual([r.method for r in self.writes()], ["PUT", "POST"])
                self.assertEqual(json.loads(self.writes()[0].data), {"image": plow_image})
                self.assertEqual(json.loads(self.writes()[1].data), {"image": index_image})
                self.assertEqual(json.loads(out)["image"], plow_image)
                self.assertIn("Plow updated", err)
                self.assertIn("Index updated", err)

    def test_promote_mirrors_the_server_normalized_pin(self):
        original = self.http

        def normalize(req, *args, **kwargs):
            response = original(req, *args, **kwargs)
            if req.method == "PUT":
                self.row["image"] = REF
                return Response(self.row)
            return response

        with patch("urllib.request.OpenerDirector.open", side_effect=normalize):
            self.run_cli("image", "promote", "hermes", REF.replace("ghcr.io", "GHCR.IO"))
        self.assertEqual(json.loads(self.writes()[1].data), {"image": REF})

    def test_plow_rejection_never_contacts_index(self):
        self.plow_status = 403
        _, err = self.run_cli("image", "promote", "hermes", REF, success=False)
        self.assertIn("you do not own hermes", err)
        self.assertTrue(all("/v1/agent-images/" in r.full_url for r in self.requests))

    def test_partial_index_failure_and_refused_image(self):
        for status, dropped in [(500, {}), (200, {"image": 1})]:
            with self.subTest(status=status):
                self.index_status, self.dropped = status, dropped
                out, err = self.run_cli("image", "promote", "hermes", REF, success=False)
                self.assertEqual(json.loads(out)["image"], REF)
                self.assertIn("Plow updated", err)
                self.assertIn("Index", err)

    def test_admin_not_index_owner_skips_successfully(self):
        self.index_status = 409
        _, err = self.run_cli("image", "promote", "hermes", REF)
        self.assertIn("Plow updated", err)
        self.assertIn("you do not own hermes on the Agent Index", err)
        self.assertIn("skipped", err)

    def test_show_uses_slug_and_needs_no_token(self):
        out, _ = self.run_cli("image", "show", "hermes", token=False)
        view = json.loads(out)
        self.assertEqual(set(view), {"plow", "index"})
        self.assertEqual(view["plow"]["image"], REF)
        self.assertEqual(view["index"]["image"], "old")
        self.assertEqual(self.requests[1].full_url, "https://index.example.test/v1/agent?agent_id=hermes")

    def test_missing_index_is_normal(self):
        self.index = None
        out, err = self.run_cli("image", "show", "hermes", token=False)
        self.assertIsNone(json.loads(out)["index"])
        self.assertIn("not on the Agent Index", err)
        self.requests.clear()
        _, err = self.run_cli("image", "promote", "hermes", REF)
        self.assertIn("not on the Agent Index", err)
        self.assertEqual([r.method for r in self.writes()], ["PUT"])

    def test_show_rejects_misconfigured_index_404(self):
        original = self.http
        for body in (b"<html>Not Found</html>", {}, {"error": "no such agent"},
                     {"ok": False, "error": None}, {"ok": True, "error": "no such agent"}):
            with self.subTest(body=body):
                def missing_route(req, *args, **kwargs):
                    if "/v1/agent?" in req.full_url:
                        return Response(body, 404)
                    return original(req, *args, **kwargs)

                with patch("urllib.request.OpenerDirector.open", side_effect=missing_route):
                    out, err = self.run_cli("image", "show", "hermes", token=False, success=False)
                self.assertEqual(out, "")
                self.assertIn("misconfigured Index base", err)
                self.assertIn("https://index.example.test/v1/agent?agent_id=hermes", err)
                self.assertIn("404", err)
                self.assertNotIn("not on the Agent Index", err)

    def test_default_index_base(self):
        with patch.dict(os.environ):
            os.environ.pop("PLOW_INDEX_BASE", None)
            self.run_cli("image", "show", "hermes", token=False, index_base=None)
        self.assertEqual(self.requests[-1].full_url,
                         "https://agent-index-server.vercel.app/v1/agent?agent_id=hermes")

    def test_set_claims_index_without_admitting_to_plow(self):
        self.row, self.index = None, None
        self.run_cli("image", "set", "hermes", "--name", "Hermes", "--blurb", "Hello")
        self.assertEqual([r.method for r in self.writes()], ["POST"])
        self.assertEqual(self.index["agent_id"], "hermes")
        self.assertEqual(self.index["name"], "Hermes")
        self.assertIsNone(self.row)

    def test_mixed_set_claims_missing_index(self):
        self.index = None
        out, _ = self.run_cli("image", "set", "hermes", "--phrase", "New phrase", "--name", "Hermes")
        self.assertEqual(json.loads(out)["phrases"], ["New phrase"])
        self.assertEqual([r.method for r in self.writes()], ["PUT", "POST"])
        self.assertEqual(self.index["name"], "Hermes")

    def test_set_routes_metadata_and_never_image(self):
        self.run_cli("image", "set", "hermes", "--name", "New", "--blurb", "Hello", "--repo", "https://example.com/repo",
                     "--video", '{"provider":"youtube","id":"demo"}', "--link", "https://example.com/start",
                     "--screenshot", "https://example.com/one.png", "--screenshot", "https://example.com/two.png")
        self.assertEqual(len(self.writes()), 1)
        body = json.loads(self.writes()[0].data)
        self.assertEqual(body, {"name": "New", "blurb": "Hello", "repo": "https://example.com/repo",
                                "video": {"provider": "youtube", "id": "demo"}, "install_url": "https://example.com/start",
                                "images": ["https://example.com/one.png", "https://example.com/two.png"]})
        self.run_cli("image", "set", "hermes", "--image", REF, success=False)
        self.assertTrue(all("image" not in json.loads(r.data) for r in self.writes()))

    def test_plow_only_set_never_contacts_index(self):
        self.run_cli("image", "set", "hermes", "--phrase", "One", "--phrase", "Two", "--owner", "owner-uid", "--disabled")
        self.assertTrue(all("/v1/agent-images/" in r.full_url for r in self.requests))
        self.assertEqual(json.loads(self.writes()[0].data), {"phrases": ["One", "Two"], "owner_uid": "owner-uid", "enabled": False})

    def test_set_never_creates_plow_row(self):
        self.row = None
        _, err = self.run_cli("image", "set", "hermes", "--name", "Hermes", "--phrase", "Hello", "--owner", "owner-uid", success=False)
        self.assertIn("not admitted to Plow yet; an admin promotes it first", err)
        self.assertFalse(self.writes())

    def test_promote_admits_with_explicit_owner(self):
        self.row = None
        out, err = self.run_cli("image", "promote", "hermes", REF, "--owner", "owner-uid")
        self.assertEqual(json.loads(self.writes()[0].data), {
            "image": REF, "name": "Site name",
            "phrases": ["Set this up for me: aiworthusing.com/agent-index/hermes"], "owner_uid": "owner-uid",
        })
        self.assertEqual(json.loads(out)["slug"], "hermes")
        self.assertIn("admitted", err)

    def test_promote_admission_refusals(self):
        cases = [
            ([], self.index, 200, "creating a Plow row requires --owner <plow user uid>; nothing was sent", ["GET"]),
            (["--owner", "owner-uid"], None, 200, "register it on the leaderboard first (image set)", ["GET", "GET"]),
            (["--owner", "owner-uid"], self.index, 404, "not admitted to Plow yet; ask an admin", ["GET", "GET", "PUT"]),
        ]
        for extra, listing, status, message, methods in cases:
            with self.subTest(message=message):
                self.row, self.index, self.plow_status = None, listing, status
                self.requests.clear()
                _, err = self.run_cli("image", "promote", "hermes", REF, *extra, success=False)
                self.assertIn(message, err)
                self.assertEqual([r.method for r in self.requests], methods)

    def test_existing_promote_ignores_owner(self):
        _, err = self.run_cli("image", "promote", "hermes", REF, "--owner", "ignored-uid")
        self.assertEqual(json.loads(self.writes()[0].data), {"image": REF})
        self.assertIn("--owner ignored", err)

    def test_push_promotes_digest_from_this_push(self):
        out, _ = self.run_cli("image", "push", "ghcr.io/example/agent:v1", "--promote", "hermes")
        self.assertEqual(self.commands, [["docker", "push", "ghcr.io/example/agent:v1"]])
        self.assertEqual(json.loads(self.writes()[0].data), {"image": REF})
        self.assertEqual(json.loads(out)["image"], REF)
        self.assertFalse(any(urllib.parse.urlsplit(r.full_url).hostname == "ghcr.io" for r in self.requests))

    def test_index_base_environment_and_local_http(self):
        with patch.dict(os.environ, {"PLOW_INDEX_BASE": "http://127.0.0.1:3847"}):
            self.run_cli("image", "show", "hermes", index_base=None)
        self.assertTrue(self.requests[-1].full_url.startswith("http://127.0.0.1:3847/"))

    def test_index_only_non_owner_fails(self):
        self.index_status = 409
        _, err = self.run_cli("image", "set", "hermes", "--name", "New", success=False)
        self.assertIn("you do not own hermes on the Agent Index", err)
        self.assertFalse(any(r.method == "PUT" for r in self.requests))

    def test_mixed_set_stops_after_plow_refusal(self):
        self.plow_status = 403
        self.run_cli("image", "set", "hermes", "--name", "New", "--phrase", "Hello", success=False)
        self.assertTrue(all("/v1/agent-images/" in r.full_url for r in self.requests))

    def test_promote_requires_exactly_one_target(self):
        self.run_cli("image", "promote", "hermes", success=False)
        self.run_cli("image", "promote", "hermes", REF, "--none", success=False)
        self.assertFalse(self.requests)

    def test_index_network_failure_reports_successful_plow_write(self):
        original = self.http

        def offline(req, *args, **kwargs):
            if "index.example.test" in req.full_url:
                raise OSError("offline")
            return original(req, *args, **kwargs)

        with patch("urllib.request.OpenerDirector.open", side_effect=offline):
            out, err = self.run_cli("image", "promote", "hermes", REF, success=False)
        self.assertEqual(json.loads(out)["image"], REF)
        self.assertIn("Plow updated", err)
        self.assertIn("offline", err)

    def test_index_base_flag_overrides_environment_and_rejects_remote_http(self):
        with patch.dict(os.environ, {"PLOW_INDEX_BASE": "https://other.example.test"}):
            self.run_cli("image", "show", "hermes")
        self.assertTrue(self.requests[-1].full_url.startswith("https://index.example.test/"))
        self.requests.clear()
        self.run_cli("image", "show", "hermes", index_base="http://index.example.test", success=False)
        self.assertFalse(self.requests)



if __name__ == "__main__":
    unittest.main()
