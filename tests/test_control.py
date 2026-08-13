import hmac
import json
import os
import queue
import shutil
import subprocess
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

import control.gp_cloud_control as control  # noqa: E402
from control.gp_cloud_control import (  # noqa: E402
    DEFAULT_APP_PORT,
    Handler,
    github_repo_from_url,
    materialize_generic_profile,
    materialize_profile,
    normalize_job,
    parse_deploy_command,
    public_action_state,
)


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

    def test_queue_marker_rejects_symlinked_marker_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue_dir = root / "queue"
            outside = root / "outside"
            queue_dir.mkdir()
            outside.mkdir()
            marker = queue_dir / "dep_1_deadbeef.deploy"
            marker.symlink_to(outside / marker.name)
            with patch.object(control, "QUEUE_DIR", queue_dir):
                with self.assertRaisesRegex(ValueError, "path"):
                    control.queue_operation("deploy", "dep_1_deadbeef")

    def test_route_files_are_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route = root / "config/caddy/routes/preview.caddy"
            with (
                patch.object(control, "ROOT", root),
                patch.object(control, "DATA", root / "data"),
                patch.object(control, "DEPLOYMENTS", root / "deployments"),
            ):
                control.atomic_route_text(route, "route\n")
            self.assertEqual(route.stat().st_mode & 0o777, 0o600)

    def test_github_api_json_rejects_untrusted_hosts(self):
        with self.assertRaises(ValueError):
            control.github_api_json("https://169.254.169.254/latest/meta-data")

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
            control.validate_vault_path("gp-cloud-evil/project")
        with self.assertRaises(ValueError):
            normalize_job(
                {
                    "repo_url": "https://github.com/owner/site.git",
                    "sha": "d" * 40,
                    "vault_path": "gp-cloud/../root",
                }
            )

    def test_deploy_command_parser_is_exact_and_conservative(self):
        accepted = ["/deploy", " /deploy", "\t/deploy\t"]
        ignored = [
            "please /deploy",
            "`/deploy`",
            "> /deploy",
            "```\n/deploy\n```",
            "/Deploy",
            "/deployment",
            "/deploy\nmore",
        ]
        unsupported = ["/deploy now", "  /deploy --force"]
        for value in accepted:
            with self.subTest(value=value):
                self.assertEqual(parse_deploy_command(value), ("deploy", None))
        for value in ignored:
            with self.subTest(value=value):
                self.assertEqual(parse_deploy_command(value)[0], "ignore")
        for value in unsupported:
            with self.subTest(value=value):
                self.assertEqual(parse_deploy_command(value)[0], "unsupported")

    def test_public_edge_is_https_and_has_separate_default_deny_hosts(self):
        root = Path(__file__).parents[1]
        caddyfile = (root / "caddy/Caddyfile").read_text(encoding="utf-8")
        self.assertIn("https://*.{$GP_CLOUD_PREVIEW_DOMAIN}", caddyfile)
        self.assertIn("dns cloudflare", caddyfile)
        self.assertIn("webhook.{$GP_CLOUD_PREVIEW_DOMAIN}", caddyfile)
        self.assertIn("actions.{$GP_CLOUD_PREVIEW_DOMAIN}", caddyfile)
        self.assertIn("control.{$GP_CLOUD_PREVIEW_DOMAIN}", caddyfile)
        self.assertIn("redir https://{host}{uri} permanent", caddyfile)
        self.assertNotIn(":{$GP_CLOUD_HTTP_PORT}", caddyfile)
        self.assertNotIn("path /metrics", caddyfile)
        self.assertNotIn("path /v1", caddyfile)

    def test_runtime_and_monitoring_installation_contracts_are_isolated(self):
        root = Path(__file__).parents[1]
        deploy = (root / "scripts/gp-cloud-deploy").read_text(encoding="utf-8")
        installer = (root / "scripts/gp-cloud-install").read_text(encoding="utf-8")
        monitoring = (root / "scripts/gp-cloud-monitoring").read_text(encoding="utf-8")
        self.assertIn('NETWORK="gp-cloud-$SLUG"', deploy)
        self.assertIn("docker network create --internal", deploy)
        self.assertIn("--memory-swap", deploy)
        self.assertNotIn("--network gp-cloud ", deploy)
        self.assertNotIn('source "$ENV_FILE"', deploy)
        self.assertIn("--enable-monitoring", installer)
        self.assertIn('"$INSTALL_ROOT/monitoring"', installer)
        self.assertIn('chmod 2750 "$CONFIG_ROOT/caddy/routes"', installer)
        self.assertIn("systemctl daemon-reload", installer)
        self.assertIn("systemctl reload caddy.service", installer)
        self.assertIn("systemctl start caddy.service", installer)
        self.assertNotIn("/etc/prometheus/prometheus.yml", installer)
        self.assertIn("127.0.0.1:9091", monitoring)
        self.assertIn("gp-cloud-prometheus.service", monitoring)

    def test_deployment_failure_alert_uses_a_time_window(self):
        root = Path(__file__).parents[1]
        alerts = (root / "monitoring/alerts.yml").read_text(encoding="utf-8")
        self.assertIn(
            "increase(gp_cloud_deployment_failures_total[1h]) > 3", alerts
        )
        self.assertNotIn('gp_cloud_deployments{state="FAILED"} > 3', alerts)

    def test_deploy_harness_rejects_metadata_health_path_injection(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "deployments/candidate/source"
            source.mkdir(parents=True)
            (source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            result = subprocess.run(
                [
                    "bash",
                    str(root / "scripts/gp-cloud-deploy"),
                    "--source",
                    str(source),
                    "--sha",
                    "a" * 40,
                    "--slug",
                    "candidate-deadbeef",
                    "--port",
                    "2222",
                    "--health-path",
                    '/ok"\n, "host_port": 22, "x": "',
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "GP_CLOUD_ROOT": directory},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("safe URL path", result.stderr)
            self.assertFalse(
                (Path(directory) / "deployments/candidate-deadbeef/metadata.json").exists()
            )

    def test_concurrent_harnesses_keep_build_and_runtime_resources_distinct(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            test_root = Path(directory)
            bin_dir = test_root / "bin"
            bin_dir.mkdir()
            command_log = test_root / "docker.log"
            docker_stub = bin_dir / "docker"
            docker_stub.write_text(
                """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "$1" == "image" && "${2:-}" == "inspect" ]]; then
  echo null
elif [[ "$1" == "port" ]]; then
  echo 127.0.0.1:31001
elif [[ "$1" == "inspect" ]]; then
  echo true
elif [[ "$1" == "run" ]]; then
  echo fake-container-id
fi
""",
                encoding="utf-8",
            )
            git_stub = bin_dir / "git"
            git_stub.write_text(
                """#!/usr/bin/env bash
value=${!#}
value=${value%\\^\\{commit\\}}
[[ "$value" =~ ^[0-9a-fA-F]{40}$ ]] && printf '%s\\n' "$value"
""",
                encoding="utf-8",
            )
            curl_stub = bin_dir / "curl"
            curl_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            for stub in (docker_stub, git_stub, curl_stub):
                stub.chmod(0o755)

            processes = []
            slugs = ("candidate-11111111", "candidate-22222222")
            for index, slug in enumerate(slugs, start=1):
                source = test_root / "deployments" / slug / "source"
                source.mkdir(parents=True)
                (source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
                processes.append(
                    subprocess.Popen(
                        [
                            "bash",
                            str(root / "scripts/gp-cloud-deploy"),
                            "--source",
                            str(source),
                            "--sha",
                            str(index) * 40,
                            "--slug",
                            slug,
                            "--port",
                            "2222",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        env={
                            **os.environ,
                            "GP_CLOUD_ROOT": directory,
                            "FAKE_DOCKER_LOG": str(command_log),
                            "PATH": f"{bin_dir}:{os.environ['PATH']}",
                        },
                    )
                )
            results = [process.communicate(timeout=20) for process in processes]
            self.assertEqual([process.returncode for process in processes], [0, 0], results)
            commands = command_log.read_text(encoding="utf-8")
            for slug in slugs:
                with self.subTest(slug=slug):
                    self.assertIn(f"buildx create --name gpcb-{slug[-8:]}", commands)
                    self.assertIn(
                        f"network create --internal --label org.gp-cloud.deployment={slug}",
                        commands,
                    )
                    self.assertIn("--network none", commands)
                    self.assertTrue((test_root / "deployments" / slug / "metadata.json").exists())

    def test_optional_monitoring_enable_disable_and_uninstall(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            test_root = Path(directory)
            installed = test_root / "opt/gp-cloud"
            (installed / "worker").mkdir(parents=True)
            (installed / "monitoring").mkdir()
            shutil.copy2(root / "scripts/gp-cloud-monitoring", installed / "worker")
            for name in ("prometheus.yml", "alerts.yml", "gp-cloud-prometheus.service"):
                shutil.copy2(root / "monitoring" / name, installed / "monitoring" / name)
            bin_dir = test_root / "bin"
            bin_dir.mkdir()
            for command in ("prometheus", "promtool", "systemctl", "curl"):
                stub = bin_dir / command
                stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
                stub.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "GP_CLOUD_TESTING": "true",
                    "GP_CLOUD_TEST_ROOT": str(test_root),
                    "PATH": f"{bin_dir}:{env['PATH']}",
                }
            )
            command = ["bash", str(installed / "worker/gp-cloud-monitoring")]
            subprocess.run([*command, "enable"], check=True, env=env, capture_output=True)
            self.assertTrue((test_root / "etc/gp-cloud/monitoring/prometheus.yml").exists())
            self.assertTrue((test_root / "etc/systemd/system/gp-cloud-prometheus.service").exists())
            subprocess.run([*command, "disable"], check=True, env=env, capture_output=True)
            subprocess.run([*command, "uninstall"], check=True, env=env, capture_output=True)
            self.assertFalse((test_root / "etc/gp-cloud/monitoring").exists())
            self.assertTrue((test_root / "var/lib/gp-cloud-prometheus").exists())

    def test_github_event_requires_created_action_and_trusted_author(self):
        payload = {
            "action": "created",
            "repository": {"full_name": "owner/site"},
            "issue": {
                "number": 7,
                "pull_request": {
                    "head": {
                        "sha": "a" * 40,
                        "repo": {
                            "full_name": "owner/site",
                            "clone_url": "https://github.com/owner/site.git",
                        },
                    }
                },
            },
            "comment": {"body": "/deploy", "author_association": "NONE"},
        }
        with self.assertRaises(PermissionError):
            control.github_event(payload, "issue_comment")
        payload["comment"]["author_association"] = "MEMBER"
        payload["action"] = "edited"
        self.assertIsNone(control.github_event(payload, "issue_comment"))

    def test_action_token_must_be_scoped_to_the_requested_repository(self):
        with patch.object(
            control,
            "github_api_json_with_token",
            return_value={"repositories": [{"full_name": "owner/site"}]},
        ):
            self.assertTrue(control.action_token_allows_repo("token", "owner/site"))
            self.assertFalse(control.action_token_allows_repo("token", "owner/other"))
class ControlHTTPIntegrationTests(unittest.TestCase):
    """Exercise the real HTTP handler against isolated temporary state."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(control, "ROOT", root))
        self.stack.enter_context(patch.object(control, "DATA", root / "data"))
        self.stack.enter_context(patch.object(control, "STATE_DIR", root / "data/deployments"))
        self.stack.enter_context(patch.object(control, "PREVIEW_DIR", root / "data/previews"))
        self.stack.enter_context(patch.object(control, "QUEUE_DIR", root / "data/queue"))
        self.stack.enter_context(patch.object(control, "TOKEN_DIR", root / "data/action-tokens"))
        self.stack.enter_context(
            patch.object(control, "FAILURE_COUNTER_FILE", root / "data/deployment-failures.json")
        )
        self.stack.enter_context(
            patch.object(control, "DELIVERY_DIR", root / "data/webhook-deliveries")
        )
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
        self.stack.enter_context(patch.object(control, "LOGIN_FAILURES", {}))
        self.stack.enter_context(patch.object(control, "JOBS", queue.Queue()))
        for directory in (
            control.STATE_DIR,
            control.PREVIEW_DIR,
            control.QUEUE_DIR,
            control.TOKEN_DIR,
            control.DELIVERY_DIR,
            control.DEPLOYMENTS,
        ):
            directory.mkdir(parents=True)
        (root / "config").mkdir()
        (root / "config/gp-cloud.env").write_text(
            "GP_CLOUD_PREVIEW_DOMAIN=preview.example.com\nGP_CLOUD_API_TOKEN=do-not-display\n",
            encoding="utf-8",
        )
        try:
            self.server = control.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            self.stack.close()
            self.temp.cleanup()
            self.skipTest("sandbox does not permit loopback sockets")
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
        self.assertNotIn("Access-Control-Allow-Origin", headers)

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

        status, _headers, body = self.request(
            "POST",
            "/ui/api/host-config",
            {"values": {"GP_CLOUD_PREVIEW_DOMAIN": "$(touch /tmp/pwned)"}},
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 400)
        self.assertIn("unsupported characters", json.loads(body)["error"])
        self.assertNotIn(
            "$(touch", (control.ROOT / "config/gp-cloud.env").read_text(encoding="utf-8")
        )

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
        self.assertTrue((control.QUEUE_DIR / f"{deployment['id']}.deploy").exists())
        self.assertTrue((control.STATE_DIR / f"{deployment['id']}.json").exists())

        status, _headers, _body = self.request("GET", "/ui/api/usage", headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_webhook_requires_hmac_signature(self):
        payload = {
            "repository": {"full_name": "owner/site"},
            "action": "created",
            "issue": {},
        }
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"webhook-secret", body, "sha256").hexdigest()
        with patch.object(control, "WEBHOOK_SECRET", "webhook-secret"):
            status, _headers, response = self.request(
                "POST",
                "/webhooks/github",
                payload,
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": signature,
                },
            )
            self.assertEqual(status, 202)
            self.assertEqual(json.loads(response), {"accepted": True})
            status, _headers, _response = self.request(
                "POST",
                "/webhooks/github",
                payload,
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-2",
                    "X-Hub-Signature-256": "sha256=bad",
                },
            )
            self.assertEqual(status, 401)

            status, _headers, _response = self.request(
                "POST",
                "/webhooks/github",
                payload,
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": signature,
                },
            )
            self.assertEqual(status, 409)

            status, _headers, _response = self.request(
                "POST",
                "/webhooks/github",
                payload,
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-fresh-id",
                    "X-Hub-Signature-256": signature,
                },
            )
            self.assertEqual(status, 409)

    def test_stop_and_usage_are_scoped_to_gp_cloud(self):
        state = {
            "id": "dep_1_deadbeef",
            "slug": "site-a1",
            "preview_slug": "site-a1",
            "runtime_slug": "site-a1-runtime",
            "preview_id": "preview_" + "a" * 20,
            "state": "RUNNING",
            "created_at": control.now(),
            "repo": "owner/site",
        }
        control.write_state(state)
        control.write_preview(
            {
                "id": state["preview_id"],
                "current_deployment_id": state["id"],
                "deployment_ids": [state["id"]],
            }
        )
        process_result = type("ProcessResult", (), {"returncode": 0, "stdout": "cleaned\n"})()
        with patch.object(control.subprocess, "run", return_value=process_result):
            requested = control.stop_deployment(state["id"])
            self.assertTrue(requested["stop_requested"])
            stopped = control.perform_stop(state["id"])
        self.assertEqual(stopped["state"], "STOPPED")
        self.assertEqual(stopped["stop_requested"], False)

        stats = "gp-cloud-site-a1\t1%\t2MiB / 3MiB\t1%\t1kB / 2kB\t3kB / 4kB\t5\nother\t9%\t9MiB\t9%\t9kB\t9kB\t9\n"
        process_result.stdout = stats
        with patch.object(control.subprocess, "run", return_value=process_result):
            usage = control.usage_metrics()
        self.assertEqual(usage["container_count"], 1)
        self.assertEqual(usage["containers"][0]["name"], "gp-cloud-site-a1")

    def test_redeploy_promotes_one_current_and_preserves_history(self):
        def candidate(sha):
            value = control.enqueue(
                {
                    "repo_url": "https://github.com/owner/site.git",
                    "sha": sha * 40,
                    "project": "site",
                    "pr_number": 42,
                },
                "test",
            )
            control.transition_state(value["id"], "BUILDING")
            return value

        completed = type("ProcessResult", (), {"returncode": 0, "stdout": "cleaned\n"})()
        first = candidate("1")
        with (
            patch.object(control, "activate_route"),
            patch.object(control, "clean_runtime", return_value=completed),
        ):
            control.promote_deployment(first["id"])
            second = candidate("2")
            control.promote_deployment(second["id"])

        first_state = control.read_state(first["id"])
        second_state = control.read_state(second["id"])
        preview = control.read_preview(second["preview_id"])
        self.assertEqual(first_state["state"], "SUPERSEDED")
        self.assertEqual(first_state["superseded_by"], second["id"])
        self.assertFalse(first_state["current"])
        self.assertTrue(second_state["current"])
        self.assertEqual(preview["current_deployment_id"], second["id"])
        self.assertEqual(len(control.deployment_records()), 2)
        self.assertNotEqual(first["runtime_slug"], second["runtime_slug"])

    def test_failed_candidate_activation_leaves_healthy_current(self):
        first = control.enqueue(
            {
                "repo_url": "https://github.com/owner/site.git",
                "sha": "3" * 40,
                "project": "site",
                "pr_number": 9,
            },
            "test",
        )
        control.transition_state(first["id"], "BUILDING")
        with patch.object(control, "activate_route"):
            control.promote_deployment(first["id"])
        replacement = control.enqueue(
            {
                "repo_url": "https://github.com/owner/site.git",
                "sha": "4" * 40,
                "project": "site",
                "pr_number": 9,
            },
            "test",
        )
        control.transition_state(replacement["id"], "BUILDING")
        with patch.object(control, "activate_route", side_effect=RuntimeError("reload failed")):
            with self.assertRaisesRegex(RuntimeError, "reload failed"):
                control.promote_deployment(replacement["id"])
        preview = control.read_preview(first["preview_id"])
        self.assertEqual(preview["current_deployment_id"], first["id"])
        self.assertEqual(control.read_state(first["id"])["state"], "RUNNING")

    def test_stop_before_worker_cancels_without_building(self):
        deployment = control.enqueue(
            {
                "repo_url": "https://github.com/owner/site.git",
                "sha": "5" * 40,
                "project": "site",
            },
            "test",
        )
        control.request_stop(deployment["id"])
        completed = type("ProcessResult", (), {"returncode": 0, "stdout": "cleaned\n"})()
        with patch.object(control, "clean_runtime", return_value=completed):
            control.run_deployment(deployment["id"])
        self.assertEqual(control.read_state(deployment["id"])["state"], "STOPPED")

    def test_restart_requeues_interrupted_build_and_preserves_identity(self):
        deployment = control.enqueue(
            {
                "repo_url": "https://github.com/owner/site.git",
                "sha": "6" * 40,
                "project": "site",
                "pr_number": 3,
            },
            "test",
        )
        control.transition_state(
            deployment["id"],
            "BUILDING",
            worker_lease_expires_at="2000-01-01T00:00:00+00:00",
        )
        with patch.object(control, "JOBS", queue.Queue()):
            control.reconcile_durable_state()
            recovered = control.read_state(deployment["id"])
            self.assertEqual(recovered["state"], "QUEUED")
            self.assertEqual(recovered["recovery_count"], 1)
            self.assertIsNone(recovered["worker_lease_expires_at"])
            self.assertEqual(recovered["runtime_slug"], deployment["runtime_slug"])
            self.assertTrue((control.QUEUE_DIR / f"{deployment['id']}.deploy").exists())

    def test_dashboard_rejects_preview_origin_mutation(self):
        cookie = self.login()
        status, _headers, body = self.request(
            "POST",
            "/ui/api/deployments/stop-all",
            {},
            headers={"Cookie": cookie, "Origin": "https://site.preview.example.com"},
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", json.loads(body)["error"])


class LifecycleRegressionTests(unittest.TestCase):
    """Exercise durable lifecycle ordering without sockets, Docker, or network access."""

    def setUp(self):
        """
        Set up isolated temporary state and control-plane dependencies for a test.
        """
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(control, "ROOT", root))
        self.stack.enter_context(patch.object(control, "DATA", root / "data"))
        self.stack.enter_context(patch.object(control, "STATE_DIR", root / "data/deployments"))
        self.stack.enter_context(patch.object(control, "PREVIEW_DIR", root / "data/previews"))
        self.stack.enter_context(patch.object(control, "QUEUE_DIR", root / "data/queue"))
        self.stack.enter_context(patch.object(control, "TOKEN_DIR", root / "data/action-tokens"))
        self.stack.enter_context(
            patch.object(control, "FAILURE_COUNTER_FILE", root / "data/deployment-failures.json")
        )
        self.stack.enter_context(patch.object(control, "DEPLOYMENTS", root / "deployments"))
        self.stack.enter_context(
            patch.object(control, "SETTINGS_FILE", root / "data/control-settings.json")
        )
        self.stack.enter_context(patch.object(control, "API_TOKEN", "test-token"))
        self.stack.enter_context(patch.object(control, "ALLOWED_REPOS", {"owner/site"}))
        self.stack.enter_context(patch.object(control, "JOBS", queue.Queue()))
        self.stack.enter_context(patch.object(control, "STOP", threading.Event()))
        for directory in (
            control.STATE_DIR,
            control.PREVIEW_DIR,
            control.QUEUE_DIR,
            control.TOKEN_DIR,
            control.DEPLOYMENTS,
        ):
            directory.mkdir(parents=True)

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    @staticmethod
    def completed(stdout="cleaned\n"):
        """
        Create a successful completed subprocess result with the specified standard output.
        
        Parameters:
            stdout (str): Standard output to include in the result.
        
        Returns:
            subprocess.CompletedProcess: A completed process result with return code 0.
        """
        return subprocess.CompletedProcess([], 0, stdout=stdout)

    def enqueue(self, sha="a", project="site", pr_number=0):
        """
        Create a test deployment for the configured repository, project, and pull request.
        
        Parameters:
            sha (str): Value used to construct the commit identifier.
            project (str): Deployment project name.
            pr_number (int): Pull request number associated with the deployment.
        
        Returns:
            The enqueued deployment result.
        """
        return control.enqueue(
            {
                "repo_url": "https://github.com/owner/site.git",
                "sha": sha * 40,
                "project": project,
                "pr_number": pr_number,
            },
            "test",
        )

    def test_long_slugs_and_cross_repository_previews_remain_unique(self):
        project = "a" * 80
        first_id, first_slug = control.preview_identity("owner/site", 17, project)
        other_id, other_slug = control.preview_identity("other/site", 17, project)
        next_pr_id, next_pr_slug = control.preview_identity("owner/site", 18, project)

        self.assertEqual(len({first_id, other_id, next_pr_id}), 3)
        self.assertEqual(len({first_slug, other_slug, next_pr_slug}), 3)
        self.assertTrue(all(len(value) <= 50 for value in (first_slug, other_slug, next_pr_slug)))

        with (
            patch.object(control.time, "time", return_value=123),
            patch.object(control.secrets, "token_hex", side_effect=["11111111", "22222222"]),
        ):
            first = self.enqueue("1", project, 17)
            second = self.enqueue("2", project, 17)
        self.assertNotEqual(first["runtime_slug"], second["runtime_slug"])
        self.assertTrue(first["runtime_slug"].endswith("-11111111"))
        self.assertTrue(second["runtime_slug"].endswith("-22222222"))
        self.assertLessEqual(len(first["runtime_slug"]), 63)
        self.assertLessEqual(len(second["runtime_slug"]), 63)

    def test_pr_build_network_requires_host_and_repository_approval(self):
        state = {"pr_number": 12, "allow_build_network": True}
        with patch.object(control, "ALLOW_PR_BUILD_NETWORK", False):
            self.assertEqual(control.approved_build_network(state), "none")
        with patch.object(control, "ALLOW_PR_BUILD_NETWORK", True):
            self.assertEqual(control.approved_build_network(state), "default")
        self.assertEqual(
            control.approved_build_network({"pr_number": 0, "allow_build_network": True}),
            "default",
        )
        self.assertEqual(
            control.approved_build_network({"pr_number": 0, "allow_build_network": False}),
            "none",
        )

    def test_pr_profiles_are_scoped_by_canonical_repository(self):
        control.SETTINGS_FILE.write_text(
            json.dumps(
                {
                    "projects": {
                        "site": {"vault_path": "gp-cloud/legacy"},
                        "owner/site": {"vault_path": "gp-cloud/owner-site"},
                    }
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            control.configured_profile({"repo": "owner/site", "project": "site", "pr_number": 1})[
                "vault_path"
            ],
            "gp-cloud/owner-site",
        )
        self.assertEqual(
            control.configured_profile({"repo": "other/site", "project": "site", "pr_number": 1}),
            {},
        )

    def test_existing_preview_slug_is_preserved_for_redeploy(self):
        preview_id, _generated_slug = control.preview_identity("owner/site", 7, "site")
        legacy_slug = "legacy-site-pr-7"
        control.write_preview(
            {
                "id": preview_id,
                "repo": "owner/site",
                "project": "site",
                "pr_number": 7,
                "slug": legacy_slug,
                "current_deployment_id": None,
                "deployment_ids": [],
                "created_at": control.now(),
            }
        )

        with patch.object(control.secrets, "token_hex", return_value="1234abcd"):
            deployment = self.enqueue("3", "site", 7)
        preview = control.read_preview(preview_id)
        self.assertEqual(preview["slug"], legacy_slug)
        self.assertEqual(deployment["preview_slug"], legacy_slug)
        self.assertEqual(deployment["slug"], legacy_slug)
        self.assertEqual(deployment["runtime_slug"], f"{legacy_slug}-1234abcd")

    def test_stop_during_build_is_observed_before_promotion(self):
        deployment = self.enqueue("4", "site", 4)
        cleaned = self.completed()

        def finish_build(*_args, **_kwargs):
            """
            Request cancellation of the deployment and report that the candidate is ready.
            
            Returns:
                tuple: A zero status code and the message ``"candidate ready\n"``.
            """
            control.request_stop(deployment["id"])
            return 0, "candidate ready\n"

        with (
            patch.object(control, "update_github_status"),
            patch.object(control, "installation_token", return_value=""),
            patch.object(control.subprocess, "run", return_value=self.completed("")),
            patch.object(control, "project_profile", side_effect=lambda state: state),
            patch.object(control, "detect_runtime_profile", side_effect=lambda _path, state: state),
            patch.object(control, "materialize_profile"),
            patch.object(control, "materialize_generic_profile"),
            patch.object(control, "run_worker_command", side_effect=finish_build),
            patch.object(control, "clean_runtime", return_value=cleaned) as clean,
            patch.object(control, "promote_deployment") as promote,
        ):
            control.run_deployment(deployment["id"])

        state = control.read_state(deployment["id"])
        self.assertEqual(state["state"], "STOPPED")
        self.assertFalse(state["stop_requested"])
        clean.assert_called_once()
        promote.assert_not_called()

    def test_stop_after_running_and_repeated_stop_are_idempotent(self):
        deployment = self.enqueue("5", "site", 5)
        control.transition_state(deployment["id"], "BUILDING")
        with patch.object(control, "activate_route"):
            control.promote_deployment(deployment["id"])

        first_request = control.request_stop(deployment["id"])
        second_request = control.request_stop(deployment["id"])
        self.assertTrue(first_request["stop_requested"])
        self.assertTrue(second_request["stop_requested"])

        with (
            patch.object(control, "clean_runtime", return_value=self.completed()) as clean,
            patch.object(control, "update_github_status"),
        ):
            stopped = control.perform_stop(deployment["id"])
            repeated_request = control.request_stop(deployment["id"])
            repeated_stop = control.perform_stop(deployment["id"])

        self.assertEqual(stopped["state"], "STOPPED")
        self.assertEqual(repeated_request["state"], "STOPPED")
        self.assertEqual(repeated_stop["state"], "STOPPED")
        self.assertIsNone(control.read_preview(deployment["preview_id"])["current_deployment_id"])
        clean.assert_called_once()

    def test_worker_continues_after_one_action_raises(self):
        first_id = "dep_100_aaaaaaaa"
        second_id = "dep_101_bbbbbbbb"
        for deployment_id in (first_id, second_id):
            control.write_state(
                {
                    "id": deployment_id,
                    "slug": deployment_id,
                    "state": "BUILDING",
                    "created_at": control.now(),
                    "logs": str(control.ROOT / "logs" / f"{deployment_id}.log"),
                }
            )
            (control.QUEUE_DIR / f"{deployment_id}.stop").write_text("stop\n", encoding="utf-8")
            control.JOBS.put(("stop", deployment_id))

        actions = []

        def perform(deployment_id):
            actions.append(deployment_id)
            if deployment_id == first_id:
                raise RuntimeError("transient cleanup failure")
            control.STOP.set()
            return control.read_state(deployment_id)

        with patch.object(control, "perform_stop", side_effect=perform):
            control.worker_loop()

        self.assertEqual(actions, [first_id, second_id])
        self.assertIn("transient cleanup failure", control.read_state(first_id)["worker_error"])
        self.assertTrue((control.QUEUE_DIR / f"{first_id}.stop").exists())
        self.assertFalse((control.QUEUE_DIR / f"{second_id}.stop").exists())

    def test_restart_preserves_preview_deployment_order(self):
        preview_id, slug = control.preview_identity("owner/site", 8, "site")
        older_id = "dep_100_ffffffff"
        newer_id = "dep_100_00000000"
        control.write_preview(
            {
                "id": preview_id,
                "repo": "owner/site",
                "project": "site",
                "pr_number": 8,
                "slug": slug,
                "current_deployment_id": None,
                "deployment_ids": [older_id, newer_id],
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        for deployment_id, generation, created_at in (
            (older_id, 1, "2026-01-01T00:00:00.000001+00:00"),
            (newer_id, 2, "2026-01-01T00:00:00.000002+00:00"),
        ):
            control.write_state(
                {
                    "id": deployment_id,
                    "repo": "owner/site",
                    "project": "site",
                    "pr_number": 8,
                    "preview_id": preview_id,
                    "preview_slug": slug,
                    "runtime_slug": f"{slug}-{deployment_id[-8:]}",
                    "slug": slug,
                    "generation": generation,
                    "state": "QUEUED",
                    "current": False,
                    "stop_requested": False,
                    "created_at": created_at,
                }
            )

        recovered_jobs = queue.Queue()
        with patch.object(control, "JOBS", recovered_jobs):
            control.reconcile_durable_state()
            queued = list(recovered_jobs.queue)

        preview = control.read_preview(preview_id)
        self.assertEqual(preview["deployment_ids"], [older_id, newer_id])
        self.assertEqual(queued, [("deploy", older_id), ("deploy", newer_id)])

    def test_promotion_crash_prefix_converges_to_committed_candidate(self):
        old = self.enqueue("6", "site", 9)
        control.transition_state(old["id"], "BUILDING")
        with patch.object(control, "activate_route"):
            control.promote_deployment(old["id"])

        candidate = self.enqueue("7", "site", 9)
        control.transition_state(candidate["id"], "BUILDING")
        with (
            patch.object(control, "activate_route"),
            patch.object(control, "write_preview", side_effect=OSError("simulated crash")),
        ):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                control.promote_deployment(candidate["id"])

        self.assertEqual(control.read_state(old["id"])["state"], "RUNNING")
        self.assertEqual(
            control.read_preview(old["preview_id"])["current_deployment_id"], old["id"]
        )
        self.assertEqual(control.read_state(candidate["id"])["state"], "RUNNING")

        recovered_jobs = queue.Queue()
        with patch.object(control, "JOBS", recovered_jobs):
            control.reconcile_durable_state()

        preview = control.read_preview(old["preview_id"])
        self.assertEqual(preview["current_deployment_id"], candidate["id"])
        self.assertEqual(control.read_state(candidate["id"])["state"], "RUNNING")
        self.assertEqual(control.read_state(old["id"])["state"], "SUPERSEDED")
        self.assertIn(("cleanup", old["id"]), list(recovered_jobs.queue))

    def test_metadata_only_deployment_remains_visible_in_history(self):
        runtime = control.DEPLOYMENTS / "legacy-runtime"
        runtime.mkdir()
        (runtime / "metadata.json").write_text(
            json.dumps(
                {
                    "slug": "legacy-runtime",
                    "sha": "8" * 40,
                    "state": "READY",
                    "created_at": "2025-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        records = control.deployment_records()
        self.assertTrue(
            any(record.get("slug") == "legacy-runtime" for record in records),
            "metadata-only legacy deployments must remain visible in history",
        )

    def test_private_clone_credentials_apply_to_clone_fetch_and_checkout(self):
        deployment = self.enqueue("9", "site", 10)
        git_calls = []

        def git_command(command, **kwargs):
            git_calls.append((command, kwargs.get("env") or {}))
            return self.completed("")

        with (
            patch.object(control, "GITHUB_TOKEN", "private-token"),
            patch.object(control, "update_github_status"),
            patch.object(control.subprocess, "run", side_effect=git_command),
            patch.object(control, "project_profile", side_effect=lambda state: state),
            patch.object(control, "detect_runtime_profile", side_effect=lambda _path, state: state),
            patch.object(control, "materialize_profile"),
            patch.object(control, "materialize_generic_profile"),
            patch.object(control, "run_worker_command", return_value=(0, "")),
            patch.object(control, "promote_deployment", side_effect=control.read_state),
        ):
            control.run_deployment(deployment["id"])

        self.assertEqual(len(git_calls), 3)
        for command, environment in git_calls:
            with self.subTest(command=command):
                self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
                self.assertIn("HOME", environment)


if __name__ == "__main__":
    unittest.main()
