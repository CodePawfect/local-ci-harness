import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
from core import HarnessError, Snapshot, read_json, write_json
import main
import sonar


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.snap = Snapshot(self.root, self.root, self.root, "hash", "commit", [])

    def test_backend_multimodule_refused(self):
        (self.root / "pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><modules><module>a</module></modules></project>')
        with self.assertRaises(HarnessError):
            main.check_backend_contract(self.snap)

    def test_backend_singlemodule_contract(self):
        (self.root / "pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"/>')
        self.assertEqual(main.check_backend_contract(self.snap)["adapter"], "single-module Maven")

    def frontend(self, scripts):
        write_json(self.root / "package.json", {"dependencies": {"next": "16.0.0"}, "scripts": scripts})
        write_json(self.root / "package-lock.json", {"lockfileVersion": 3})

    def test_frontend_missing_lint_is_error(self):
        self.frontend({"build": "next build"})
        with self.assertRaises(HarnessError):
            main.check_frontend_contract(self.snap, True)

    def test_frontend_quick_contract(self):
        self.frontend({k: "command" for k in ["lint", "typecheck", "test:unit"]})
        self.assertEqual(main.check_frontend_contract(self.snap, False)["package_manager"], "npm")

    def test_gitleaks_redacts_before_raising(self):
        path = self.root / "leaks.json"
        write_json(path, [{"Secret": "do-not-copy", "Match": "do-not-copy", "File": "example.ts"}])
        with self.assertRaises(HarnessError):
            main.redact_and_check_gitleaks(path)
        self.assertNotIn("do-not-copy", path.read_text())
        self.assertEqual(read_json(path)[0]["File"], "example.ts")

    def test_clean_gitleaks(self):
        path = self.root / "leaks.json"
        write_json(path, [])
        self.assertEqual(main.redact_and_check_gitleaks(path)["findings"], 0)

    def test_production_zap_target_refused(self):
        with self.assertRaises(HarnessError):
            main.validate_zap_url("https://example.com/")

    def test_localhost_zap_wrong_network_refused(self):
        with self.assertRaises(HarnessError):
            main.validate_zap_url("http://localhost:3000")

    def test_docker_zap_target_allowed(self):
        self.assertEqual(main.validate_zap_url("http://frontend:3000"), "http://frontend:3000")

    def test_zap_url_credentials_refused(self):
        with self.assertRaises(HarnessError):
            main.validate_zap_url("http://admin:secret@frontend:3000")

    def test_empty_zap_report_blocks(self):
        path = self.root / "zap.json"
        write_json(path, {"site": []})
        with self.assertRaises(HarnessError):
            main.validate_zap_report(path)

    def test_node_junit_is_injected_via_node_options_before_script_arguments(self):
        class FakeRunner:
            def __init__(self):
                self.calls = []

            def blocked(self, name, reason):
                raise AssertionError(f"unexpected block: {name}: {reason}")

            def docker(self, name, image, args, snap, **kwargs):
                self.calls.append({"name": name, "image": image, "args": args, "kwargs": kwargs})
                return True

        profile = {
            "commands": {"test": ["npm", "run", "test"]},
            "evidence": {"tests": "test-results"},
            "environment": {"NODE_OPTIONS": "--max-old-space-size=512"},
            "test_reporter": "node-junit",
        }
        runner = FakeRunner()
        with patch("main.cache", return_value=self.root / "cache"):
            self.assertTrue(main.run_profile_command(
                runner, profile, self.snap, "project-tests", "test"
            ))

        call = runner.calls[0]
        self.assertEqual(call["args"], ["run", "test"])
        self.assertIn("--max-old-space-size=512", call["kwargs"]["env"]["NODE_OPTIONS"])
        self.assertIn("--test-reporter=junit", call["kwargs"]["env"]["NODE_OPTIONS"])
        self.assertIn(
            "--test-reporter-destination=test-results/TEST-node.xml",
            call["kwargs"]["env"]["NODE_OPTIONS"],
        )
        self.assertTrue((self.root / "test-results").is_dir())

    def test_playwright_command_gets_junit_and_isolated_artifacts(self):
        class FakeRunner:
            def __init__(self):
                self.calls = []

            def blocked(self, name, reason):
                raise AssertionError(f"unexpected block: {name}: {reason}")

            def docker(self, name, image, args, snap, **kwargs):
                self.calls.append({"name": name, "image": image, "args": args, "kwargs": kwargs})
                return True

        profile = {
            "commands": {"e2e": ["npm", "run", "test:e2e"]},
            "evidence": {"e2e": "test-results"},
            "environment": {},
        }
        runner = FakeRunner()
        with patch("main.cache", return_value=self.root / "cache"):
            self.assertTrue(main.run_profile_command(
                runner,
                profile,
                self.snap,
                "project-e2e-tests",
                "e2e",
                image_override="playwright",
                extra_args=["--reporter=list,junit,html", "--output", "test-results/e2e-artifacts"],
                extra_env={
                    "PLAYWRIGHT_JUNIT_OUTPUT_FILE": "test-results/TEST-playwright.xml",
                    "PLAYWRIGHT_HTML_OPEN": "never",
                },
            ))

        call = runner.calls[0]
        self.assertEqual(
            call["args"],
            ["run", "test:e2e", "--", "--reporter=list,junit,html", "--output", "test-results/e2e-artifacts"],
        )
        self.assertEqual(
            call["kwargs"]["env"]["PLAYWRIGHT_JUNIT_OUTPUT_FILE"],
            "test-results/TEST-playwright.xml",
        )
        self.assertEqual(call["kwargs"]["env"]["PLAYWRIGHT_HTML_OPEN"], "never")

    def test_colima_runtime_starts_profile_and_selects_its_socket(self):
        status = type("Process", (), {"returncode": 0, "stdout": '{"name":"default","status":"Stopped"}', "stderr": ""})()
        started = type("Process", (), {"returncode": 0})()
        with patch("main.shutil.which", return_value="/opt/homebrew/bin/tool"), \
             patch("main.subprocess.run", side_effect=[status, started]) as run, \
             patch("main.capture", return_value="29.2.1"), \
             patch.dict(os.environ, {"DOCKER_HOST": "unix:///wrong.sock", "DOCKER_CONTEXT": "desktop"}, clear=False):
            main.ensure_docker({
                "container_runtime": {
                    "provider": "colima",
                    "profile": "default",
                    "auto_start": True,
                }
            })
            selected_host = os.environ["DOCKER_HOST"]
            selected_context = os.environ.get("DOCKER_CONTEXT")

        self.assertEqual(
            run.call_args_list[1].args[0],
            ["colima", "start", "-p", "default", "--runtime", "docker"],
        )
        self.assertEqual(
            selected_host,
            f"unix://{Path.home()}/.colima/default/docker.sock",
        )
        self.assertIsNone(selected_context)

    def test_sonar_runtime_preflight_rejects_small_docker_memory(self):
        with patch("main.docker_memory_bytes", return_value=4 * 1024 ** 3):
            with self.assertRaisesRegex(HarnessError, "Increase Colima/Docker memory"):
                main.sonar_runtime_requirements({})

    def test_sonar_runtime_preflight_reports_scanner_limit(self):
        with patch("main.docker_memory_bytes", return_value=8 * 1024 ** 3):
            result = main.sonar_runtime_requirements({})
        self.assertEqual(result["docker_memory"], "8.0 GiB")
        self.assertEqual(result["required_memory"], "8.0 GiB")
        self.assertEqual(result["scanner_container_memory"], "6g")

    def test_zap_evidence_has_scanned_site(self):
        path = self.root / "zap.json"
        write_json(path, {"site": [{"@name": "http://frontend:3000"}]})
        self.assertEqual(main.validate_zap_report(path)["sites"], 1)

    def test_artifact_symlink_not_followed(self):
        coverage = self.root / "coverage"
        coverage.mkdir()
        (coverage / "escape").symlink_to("/etc/passwd")
        fake_runner = type("R", (), {"reports": self.root / "reports"})()
        with self.assertRaises(HarnessError):
            main.copy_evidence(fake_runner, "frontend", self.snap)

    @patch("main.ROOT")
    def test_init_does_not_overwrite_existing(self, mocked):
        # Use a real Path as the patched module root, not mock filesystem methods.
        with patch.object(main, "ROOT", self.root), contextlib.redirect_stdout(io.StringIO()):
            (self.root / "harness.example.json").write_text('{"example": true}')
            main.initialize()
            original_env = (self.root / ".env").read_text()
            (self.root / "harness.json").write_text('{"custom": true}')
            main.initialize()
            self.assertEqual((self.root / ".env").read_text(), original_env)
            self.assertEqual(read_json(self.root / "harness.json"), {"custom": True})
            self.assertEqual((self.root / ".env").stat().st_mode & 0o777, 0o600)

    def test_profile_bootstrap_does_not_require_legacy_project_config(self):
        repo = self.root / "repo"
        repo.mkdir()
        (self.root / ".local").mkdir()
        profile = {"project": {"adapter": "custom"}}
        with patch.object(main, "ROOT", self.root), \
             patch.object(main, "load_install_config", return_value={"images": {}}), \
             patch.object(main, "load_config", side_effect=AssertionError("legacy config must not be loaded")), \
             patch.object(main, "repository_root", return_value=repo), \
             patch.object(main, "load_profile", return_value=(profile, repo / ".ci/harness.json")), \
             patch.object(main, "sonar_url", return_value="http://127.0.0.1:9000"), \
             patch.object(main, "provision") as provision, \
             patch.object(main.sys, "argv", ["ci", "bootstrap", "--repo", str(repo)]):
            self.assertEqual(main.main(), 0)
        provision.assert_called_once_with(self.root, {"images": {}}, "http://127.0.0.1:9000", profile_repo=repo)

    def test_sonar_credentials_command_prints_read_only_ui_access(self):
        (self.root / ".local").mkdir()
        (self.root / ".local/sonar-reader.password").write_text("reader-secret!Aa1\n")
        output = io.StringIO()
        with patch.object(main, "ROOT", self.root), \
             patch.object(main, "load_install_config", return_value={"images": {}}), \
             patch.object(main, "local_env", return_value={"SONAR_PORT": "9100"}), \
             patch.object(main.sys, "argv", ["ci", "sonar", "credentials"]), \
             contextlib.redirect_stdout(output):
            self.assertEqual(main.main(), 0)
        text = output.getvalue()
        self.assertIn("URL: http://127.0.0.1:9100", text)
        self.assertIn("Username: local-harness-reader", text)
        self.assertIn("Password: reader-secret!Aa1", text)

class SonarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        (self.root / ".local").mkdir()
        (self.root / ".local/sonar-reader.token").write_text("test-reader-token")
        self.out = self.root / "reports"
        self.out.mkdir()
        self.task = self.out / "report-task.txt"
        self.task.write_text("ceTaskId=task-123\nserverUrl=http://do-not-trust-metadata.invalid\n")

    def test_gate_name_api(self):
        client = sonar.Sonar("http://localhost:9000")
        with patch.object(client, "parameters", return_value={"gateName"}), patch.object(client, "post", return_value={}) as post:
            client.gate_post("qualitygates/select", {"name": "policy", "id": "1"}, projectKey="backend")
            self.assertEqual(post.call_args.kwargs["gateName"], "policy")

    def test_gate_legacy_api(self):
        client = sonar.Sonar("http://localhost:9000")
        with patch.object(client, "parameters", return_value={"gateId"}), patch.object(client, "post", return_value={}) as post:
            client.gate_post("qualitygates/select", {"name": "policy", "id": "1"})
            self.assertEqual(post.call_args.kwargs["gateId"], "1")

    def test_unknown_gate_api_fails_closed(self):
        client = sonar.Sonar("http://localhost:9000")
        with patch.object(client, "parameters", return_value=set()), self.assertRaises(HarnessError):
            client.gate_post("qualitygates/select", {"name": "policy"})

    def test_reader_preserves_sonar_default_group_but_removes_extra_groups(self):
        class FakeSonar:
            def __init__(self):
                self.removed = []
                self.created_password = None

            def call(self, endpoint, params=None, post=False):
                if endpoint == "users/search":
                    return {"users": []}
                if endpoint == "users/groups":
                    return {"groups": [
                        {"name": "sonar-users", "selected": True},
                        {"name": "custom-group", "selected": True},
                    ]}
                return {}

            def post(self, endpoint, **params):
                if endpoint == "users/create":
                    self.created_password = params["password"]
                if endpoint == "user_groups/remove_user":
                    self.removed.append(params["name"])
                if endpoint == "user_tokens/generate":
                    return {"token": "reader-token"}
                return {}

        client = FakeSonar()
        self.assertEqual(sonar._ensure_reader(client, self.root), "local-harness-reader")
        self.assertEqual(client.removed, ["custom-group"])
        self.assertEqual((self.root / ".local/sonar-reader.token").read_text(), "reader-token\n")
        reader_password = self.root / ".local/sonar-reader.password"
        self.assertTrue(reader_password.is_file())
        self.assertEqual(reader_password.read_text(), client.created_password + "\n")
        self.assertEqual(reader_password.stat().st_mode & 0o777, 0o600)

    def test_reader_password_is_reset_for_legacy_reader_without_secret_file(self):
        class FakeSonar:
            def __init__(self):
                self.password = None

            def call(self, endpoint, params=None, post=False):
                if endpoint == "users/search":
                    return {"users": [{"login": "local-harness-reader", "local": True}]}
                if endpoint == "users/groups":
                    return {"groups": []}
                return {}

            def post(self, endpoint, **params):
                if endpoint == "users/change_password":
                    self.password = params["password"]
                if endpoint == "user_tokens/generate":
                    return {"token": "reader-token"}
                return {}

        client = FakeSonar()
        self.assertEqual(sonar._ensure_reader(client, self.root), "local-harness-reader")
        reader_password = self.root / ".local/sonar-reader.password"
        self.assertEqual(reader_password.read_text(), client.password + "\n")
        self.assertEqual(reader_password.stat().st_mode & 0o777, 0o600)

    def test_print_ui_credentials_uses_read_only_account(self):
        password = self.root / ".local/sonar-reader.password"
        password.write_text("reader-secret!Aa1\n")
        password.chmod(0o600)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sonar.print_ui_credentials(self.root, "http://127.0.0.1:9000")
        text = output.getvalue()
        self.assertIn("URL: http://127.0.0.1:9000", text)
        self.assertIn("Username: local-harness-reader", text)
        self.assertIn("Password: reader-secret!Aa1", text)

    @patch("sonar.Sonar")
    def test_exact_analysis_id_and_local_endpoint_used(self, cls):
        client = cls.return_value
        client.call.side_effect = [
            {"task": {"status": "SUCCESS", "analysisId": "analysis-456"}},
            {"projectStatus": {"status": "OK"}},
            {"issues": [], "paging": {"total": 0}},
        ]
        result = sonar.export_analysis(self.root, self.out, self.task, "backend", "http://127.0.0.1:9000")
        self.assertEqual(result["analysis_id"], "analysis-456")
        self.assertEqual(client.call.call_args_list[1].args[1], {"analysisId": "analysis-456"})
        self.assertEqual(cls.call_args.args[0], "http://127.0.0.1:9000")

    @patch("sonar.Sonar")
    def test_failed_quality_gate_exports_evidence_and_fails(self, cls):
        cls.return_value.call.side_effect = [
            {"task": {"status": "SUCCESS", "analysisId": "analysis-456"}},
            {"projectStatus": {"status": "ERROR"}},
            {"issues": [{"key": "issue"}], "paging": {"total": 1}},
        ]
        with self.assertRaises(HarnessError):
            sonar.export_analysis(self.root, self.out, self.task, "backend", "http://127.0.0.1:9000")
        self.assertTrue((self.out / "sonar-quality-gate.json").is_file())
        self.assertTrue((self.out / "sonar-issues.json").is_file())

    def test_no_task_file_is_not_previous_analysis_pass(self):
        self.task.unlink()
        with self.assertRaises(HarnessError):
            sonar.export_analysis(self.root, self.out, self.task, "backend", "http://127.0.0.1:9000")

    def test_profile_bootstrap_derives_project_key_languages_and_token(self):
        repo = self.root / "saas"
        (repo / ".ci").mkdir(parents=True)
        write_json(repo / ".ci" / "harness.json", {
            "schema_version": 2,
            "project": {
                "name": "Acme SaaS",
                "adapter": "next-fullstack",
                "subdir": ".",
            },
            "target_branch": "main",
            "stages": ["secrets", "sonar"],
            "commands": {},
            "evidence": {},
            "thresholds": {"lines": 70, "branches": 60},
            "environment": {},
        })

        class FakeSonar:
            def __init__(self, *_args, **_kwargs):
                self.calls = []
                self.token_number = 0

            def call(self, endpoint, params=None, post=False):
                self.calls.append(("call", endpoint, params, post))
                if endpoint == "authentication/validate":
                    return {"valid": True}
                if endpoint == "qualitygates/list":
                    return {"qualitygates": []}
                if endpoint == "qualitygates/show":
                    return {"conditions": []}
                if endpoint == "metrics/search":
                    return {"metrics": [
                        {"key": "new_bugs"},
                        {"key": "new_vulnerabilities"},
                        {"key": "new_coverage"},
                    ]}
                if endpoint == "settings/values":
                    return {"settings": []}
                if endpoint == "users/search":
                    return {"users": []}
                if endpoint == "users/groups":
                    return {"groups": []}
                if endpoint == "projects/search":
                    return {"components": []}
                if endpoint == "qualityprofiles/search":
                    return {"profiles": [{
                        "name": "Sonar way",
                        "isBuiltIn": True,
                        "key": "sonar-way",
                        "activeRuleCount": 123,
                    }]}
                return {}

            def post(self, endpoint, **params):
                self.calls.append(("post", endpoint, params, True))
                if endpoint == "qualitygates/create":
                    return {"id": "gate-1"}
                if endpoint == "qualityprofiles/copy":
                    return {"profile": {
                        "name": params["toName"],
                        "key": "local-profile",
                        "activeRuleCount": 123,
                    }}
                if endpoint == "user_tokens/generate":
                    self.token_number += 1
                    return {"token": f"token-{self.token_number}"}
                return {}

            def gate_post(self, endpoint, gate, **params):
                self.calls.append(("gate_post", endpoint, {**params, "gate": gate}, True))
                return {}

        client = FakeSonar()
        config = {"sonar": {"new_code_coverage": 80, "new_code_days": 30}}
        with patch("sonar.wait_ready"), patch("sonar.Sonar", return_value=client), \
             patch("builtins.input", return_value="admin"), \
             patch("sonar.getpass.getpass", return_value="changed-password"):
            sonar.provision(self.root, config, "http://127.0.0.1:9000", profile_repo=repo)

        manifest = read_json(self.root / ".local" / "sonar-policy.json")
        project = manifest["profile_projects"]["acme-saas"]
        self.assertEqual(manifest["projects"]["acme-saas"], "acme-saas")
        self.assertEqual(project["project_key"], "acme-saas")
        self.assertEqual(project["languages"], ["js", "ts"])
        self.assertEqual(project["target_branch"], "main")
        self.assertEqual(project["token_file"], ".local/sonar-acme-saas.token")
        self.assertNotIn("token-2", json.dumps(manifest))
        token_path = self.root / project["token_file"]
        self.assertEqual(token_path.read_text(), "token-2\n")
        self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
        admin_password = self.root / ".local/sonar-admin.password"
        self.assertTrue(admin_password.is_file())
        self.assertNotEqual(admin_password.read_text().strip(), "admin")
        self.assertEqual(admin_password.stat().st_mode & 0o777, 0o600)
        self.assertTrue(any(
            kind == "post" and endpoint == "users/change_password"
            and params["login"] == "admin"
            and params["previousPassword"] == "admin"
            for kind, endpoint, params, _ in client.calls
        ))
        self.assertTrue(any(
            kind == "gate_post" and endpoint == "qualitygates/select"
            and params["projectKey"] == "acme-saas"
            for kind, endpoint, params, _ in client.calls
        ))
        self.assertTrue(any(
            kind == "post" and endpoint == "new_code_periods/set"
            and params["project"] == "acme-saas"
            and params["type"] == "REFERENCE_BRANCH"
            and params["value"] == "main"
            for kind, endpoint, params, _ in client.calls
        ))


if __name__ == "__main__":
    unittest.main()
