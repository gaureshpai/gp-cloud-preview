import os
import hmac
import json
import queue
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("GP_CLOUD_API_TOKEN", "test-token")
os.environ.setdefault("GP_CLOUD_ALLOWED_REPOS", "owner/site")

from control.gp_cloud_control import (  # noqa: E402
    Handler,
    github_repo_from_url,
    materialize_generic_profile,
    materialize_profile,
    normalize_job,
    public_action_state,
    DEFAULT_APP_PORT,
)
import control.gp_cloud_control as control  # noqa: E402


class ControlContractTests(unittest.TestCase):
    def test_docker_fixtures_match_the_default_port_contract(self):
        fixture_root = Path(__file__).parents[1] / "fixtures"
        smoke = (fixture_root / "smoke-app/Dockerfile").read_text(encoding="utf-8")
        failing = (fixture_root / "failing-health-app/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("EXPOSE 2222", smoke)
        self.assertIn('"-p", "2222"', smoke)
        self.assertIn("EXPOSE 2222", failing)
        self.assertIn(
            "failing-health-app", (fixture_root / "README.md").read_text(encoding="utf-8")
        )

    def test_github_url_normalization_supports_https_and_ssh(self):
        self.assertEqual(github_repo_from_url("https://github.com/owner/site.git"), "owner/site")
        self.assertEqual(github_repo_from_url("git@github.com:owner/site.git"), "owner/site")
        with self.assertRaises(ValueError):
            github_repo_from_url("https://github.com.evil.test/owner/site.git")

    def test_deployment_requires_full_commit_sha(self):
        job = normalize_job(
            {
                "repo_url": "https://github.com/owner/site.git",
                "sha": "a" * 40,
                "project": "site",
            }
        )
        self.assertEqual(job["repo"], "owner/site")
        self.assertEqual(job["app_port"], DEFAULT_APP_PORT)
        with self.assertRaises(ValueError):
            normalize_job({"repo_url": "https://github.com/owner/site.git", "sha": "a" * 7})

    def test_public_state_does_not_expose_action_token_metadata(self):
        value = public_action_state(
            {"id": "dep", "clone_token_file": "/secret", "action_token_hash": "hash"}
        )
        self.assertNotIn("clone_token_file", value)
        self.assertNotIn("action_token_hash", value)

    def test_next_profile_materializes_a_static_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "package.json").write_text("{}", encoding="utf-8")
            (source / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            materialize_profile(source, {"profile": "next-static"})
            dockerfile = (source / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("COPY --from=build /app/out", dockerfile)
            self.assertIn("EXPOSE 2222", dockerfile)
            self.assertTrue((source / "nginx.conf").exists())

    def test_generic_runtime_profiles_materialize_locked_builds(self):
        cases = {
            "uv": ["pyproject.toml", "uv.lock"],
            "python": ["requirements.txt"],
            "npm": ["package.json", "package-lock.json"],
            "pnpm": ["package.json", "pnpm-lock.yaml"],
        }
        for runtime, files in cases.items():
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                for filename in files:
                    (source / filename).write_text("{}\n", encoding="utf-8")
                materialize_generic_profile(
                    source, {"runtime": runtime, "start_command": "echo ready"}
                )
                dockerfile = (source / "Dockerfile").read_text(encoding="utf-8")
                self.assertIn("FROM", dockerfile)
            self.assertIn("echo ready", dockerfile)

    def test_vault_path_is_preserved_for_runtime_environment_injection(self):
        job = normalize_job(
            {
                "repo_url": "https://github.com/owner/site.git",
                "sha": "b" * 40,
                "project": "site",
                "vault_path": "gp-cloud/projects/site",
            }
        )
        self.assertEqual(job["vault_path"], "gp-cloud/projects/site")
        with self.assertRaises(ValueError):
            normalize_job(
                {
                    "repo_url": "https://github.com/owner/site.git",
                    "sha": "b" * 40,
                    "project": "site",
                    "vault_path": "secret/root",
                }
            )

    def test_empty_repository_allowlist_fails_closed(self):
        with patch.object(control, "ALLOWED_REPOS", set()):
            with self.assertRaises(PermissionError):
                normalize_job(
                    {
                        "repo_url": "https://github.com/owner/site.git",
                        "sha": "c" * 40,
                        "project": "site",
                    }
                )

    def test_github_app_still_requires_repository_allowlist(self):
        payload = {
            "repository": {"full_name": "owner/site"},
            "installation": {"id": 42},
            "issue": {},
        }
        with (
            patch.object(control, "ALLOWED_REPOS", set()),
            patch.object(control, "GITHUB_INSTALLATION_ID", "42"),
        ):
            with self.assertRaises(PermissionError):
                control.github_event(payload, "issue_comment")

    def test_validation_rejects_unsafe_health_and_vault_values(self):
        with self.assertRaises(ValueError):
            normalize_job(
                {
                    "repo_url": "https://github.com/owner/site.git",
                    "sha": "d" * 40,
                    "health_path": "health",
                }
            )
        with self.assertRaises(ValueError):
            normalize_job(
                {
                    "repo_url": "https://github.com/owner/site.git",
                    "sha": "d" * 40,
                    "vault_path": "gp-cloud/../root",
                }
            )


class ControlHTTPIntegrationTests(unittest.TestCase):
    """Exercise the real HTTP handler against isolated temporary state."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(control, "ROOT", root))
        self.stack.enter_context(patch.object(control, "DATA", root / "data"))
        self.stack.enter_context(patch.object(control, "STATE_DIR", root / "data/deployments"))
        self.stack.enter_context(patch.object(control, "QUEUE_DIR", root / "data/queue"))
        self.stack.enter_context(patch.object(control, "TOKEN_DIR", root / "data/action-tokens"))
        self.stack.enter_context(patch.object(control, "DEPLOYMENTS", root / "deployments"))
        self.stack.enter_context(
            patch.object(control, "SETTINGS_FILE", root / "data/settings.json")
        )
        self.stack.enter_context(patch.object(control, "API_TOKEN", "test-token"))
        self.stack.enter_context(patch.object(control, "ADMIN_PASSWORD", "admin-password"))
        self.stack.enter_context(patch.object(control, "ALLOWED_REPOS", {"owner/site"}))
        self.stack.enter_context(patch.object(control, "VAULT_ADDR", ""))
        self.stack.enter_context(patch.object(control, "VAULT_TOKEN", ""))
        self.stack.enter_context(patch.object(control, "VAULT_TOKEN_FILE", ""))
        self.stack.enter_context(patch.object(control, "SESSIONS", {}))
        self.stack.enter_context(patch.object(control, "JOBS", queue.Queue()))
        for directory in (
            control.STATE_DIR,
            control.QUEUE_DIR,
            control.TOKEN_DIR,
            control.DEPLOYMENTS,
        ):
            directory.mkdir(parents=True)
        (root / "config").mkdir()
        (root / "config/gp-cloud.env").write_text(
            "GP_CLOUD_PREVIEW_DOMAIN=preview.example.com\nGP_CLOUD_API_TOKEN=do-not-display\n",
            encoding="utf-8",
        )
        self.server = control.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.stack.close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        data = None
        request_headers = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode()
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=request_headers
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def login(self):
        encoded = urllib.parse.urlencode({"password": "admin-password"}).encode()
        request = urllib.request.Request(self.base_url + "/ui/login", data=encoded, method="POST")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, new_url):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 303)
            return error.headers.get("Set-Cookie", "").split(";", 1)[0]
        self.fail("login did not redirect")

    def test_public_health_and_private_mutation(self):
        status, headers, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ok"], True)
        self.assertEqual(headers["Cache-Control"], "no-store")

        status, _headers, body = self.request(
            "POST",
            "/v1/deployments",
            {
                "repo_url": "https://github.com/owner/site.git",
                "sha": "e" * 40,
                "project": "site",
            },
        )
        self.assertEqual(status, 401)
        self.assertIn("unauthorized", json.loads(body)["error"])

    def test_login_session_and_host_config_editor(self):
        cookie = self.login()
        status, _headers, body = self.request("GET", "/ui", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b"Host configuration", body)

        status, _headers, body = self.request(
            "GET", "/ui/api/host-config", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        inventory = json.loads(body)
        self.assertTrue(inventory["available"])
        self.assertIsNone(
            next(item for item in inventory["keys"] if item["name"] == "GP_CLOUD_API_TOKEN")[
                "value"
            ]
        )

        status, _headers, _body = self.request(
            "POST",
            "/ui/api/host-config",
            {"values": {"GP_CLOUD_PREVIEW_DOMAIN": "example.org"}},
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertIn(
            "GP_CLOUD_PREVIEW_DOMAIN=example.org",
            (control.ROOT / "config/gp-cloud.env").read_text(encoding="utf-8"),
        )

        status, _headers, body = self.request(
            "POST",
            "/ui/api/host-config",
            {"values": {"GP_CLOUD_API_TOKEN": "replace-me"}},
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 403)
        self.assertIn("local-only", json.loads(body)["error"])

    def test_authenticated_deployment_is_queued_and_public_state_is_redacted(self):
        cookie = self.login()
        status, _headers, body = self.request(
            "POST",
            "/v1/deployments",
            {
                "repo_url": "https://github.com/owner/site.git",
                "sha": "f" * 40,
                "project": "site",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        self.assertEqual(status, 202)
        deployment = json.loads(body)
        self.assertEqual(deployment["state"], "QUEUED")
        self.assertNotIn("vault_path", deployment)

        status, _headers, body = self.request("GET", "/v1/deployments")
        self.assertEqual(status, 200)
        self.assertNotIn("owner/site", body.decode())

        status, _headers, body = self.request(
            "GET", "/v1/deployments", headers={"Authorization": "Bearer test-token"}
        )
        self.assertEqual(status, 200)
        self.assertIn("owner/site", body.decode())
        self.assertTrue((control.QUEUE_DIR / deployment["id"]).exists())
        self.assertTrue((control.STATE_DIR / f"{deployment['id']}.json").exists())

        status, _headers, _body = self.request("GET", "/ui/api/usage", headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_webhook_requires_hmac_signature(self):
        payload = {
            "repository": {"full_name": "owner/site"},
            "issue": {},
        }
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"webhook-secret", body, "sha256").hexdigest()
        with patch.object(control, "WEBHOOK_SECRET", "webhook-secret"):
            status, _headers, response = self.request(
                "POST",
                "/webhooks/github",
                payload,
                headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": signature},
            )
            self.assertEqual(status, 202)
            self.assertEqual(json.loads(response), {"accepted": True})
            status, _headers, _response = self.request(
                "POST",
                "/webhooks/github",
                payload,
                headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": "sha256=bad"},
            )
            self.assertEqual(status, 401)

    def test_stop_and_usage_are_scoped_to_gp_cloud(self):
        state = {
            "id": "dep_test",
            "slug": "site-a1",
            "state": "RUNNING",
            "created_at": control.now(),
            "repo": "owner/site",
        }
        control.write_state(state)
        (control.QUEUE_DIR / "dep_test").write_text("queued\n", encoding="utf-8")
        process_result = type("ProcessResult", (), {"returncode": 0, "stdout": "cleaned\n"})()
        with patch.object(control.subprocess, "run", return_value=process_result):
            stopped = control.stop_deployment("dep_test")
        self.assertEqual(stopped["state"], "STOPPED")
        self.assertEqual(stopped["stop_requested"], False)

        stats = "gp-cloud-site-a1\t1%\t2MiB / 3MiB\t1%\t1kB / 2kB\t3kB / 4kB\t5\nother\t9%\t9MiB\t9%\t9kB\t9kB\t9\n"
        process_result.stdout = stats
        with patch.object(control.subprocess, "run", return_value=process_result):
            usage = control.usage_metrics()
        self.assertEqual(usage["container_count"], 1)
        self.assertEqual(usage["containers"][0]["name"], "gp-cloud-site-a1")


if __name__ == "__main__":
    unittest.main()
