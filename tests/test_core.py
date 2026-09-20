import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
from core import (HarnessError, InfrastructureError, Runner, capture, format_memory,
                  frontend_coverage, git, jacoco_coverage, memory_bytes, read_json,
                  require_junit, safe_relative, snapshot, source_files, validate_semgrep,
                  validate_trivy, verify_unchanged, write_json)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def file(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def json(self, name, data):
        path = self.root / name
        write_json(path, data)
        return path


class SnapshotTests(TempCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "source"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Harness Tests")
        git(self.repo, "config", "user.email", "tests@example.invalid")
        self.file("source/main.txt", "original")
        self.file("source/.gitignore", ".env\nignored/\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "initial")

    def snap(self, history=False):
        return snapshot(self.repo, self.root / "copy", ".", history)

    def test_modified_and_untracked_included(self):
        self.file("source/main.txt", "modified")
        self.file("source/new.txt", "not yet committed")
        snap = self.snap()
        self.assertEqual((snap.app / "main.txt").read_text(), "modified")
        self.assertTrue((snap.app / "new.txt").is_file())

    def test_gitignored_secret_not_copied(self):
        self.file("source/.env", "local only")
        self.assertFalse((self.snap().root / ".env").exists())

    def test_tracked_ignored_file_is_copied(self):
        self.file("source/.env", "tracked mistake")
        git(self.repo, "add", "-f", ".env")
        self.assertEqual((self.snap().root / ".env").read_text(), "tracked mistake")

    def test_generated_outputs_not_reused(self):
        self.file("source/target/report.xml", "stale")
        self.file("source/node_modules/cache.js", "stale")
        snap = self.snap()
        self.assertFalse((snap.root / "target").exists())
        self.assertFalse((snap.root / "node_modules").exists())

    def test_deleted_tracked_file_stays_deleted(self):
        (self.repo / "main.txt").unlink()
        self.assertFalse((self.snap(True).root / "main.txt").exists())

    def test_full_clone_has_independent_history(self):
        snap = self.snap(True)
        self.assertEqual(git(snap.root, "rev-parse", "HEAD"), snap.commit)
        self.assertFalse((snap.root / ".git/objects/info/alternates").exists())

    def test_linked_worktree_supported(self):
        worktree = self.root / "worktree"
        git(self.repo, "worktree", "add", "--detach", str(worktree), "HEAD")
        (worktree / "main.txt").write_text("worktree edit")
        snap = snapshot(worktree, self.root / "copy", ".", True)
        self.assertEqual((snap.root / "main.txt").read_text(), "worktree edit")
        self.assertTrue((snap.root / ".git").is_dir())

    def test_external_symlink_refused(self):
        external = self.file("outside", "secret")
        (self.repo / "escape").symlink_to(external)
        with self.assertRaises(HarnessError):
            self.snap()

    def test_internal_absolute_symlink_rewritten(self):
        (self.repo / "link.txt").symlink_to(self.repo / "main.txt")
        snap = self.snap()
        self.assertFalse(os.path.isabs(os.readlink(snap.root / "link.txt")))
        self.assertEqual((snap.root / "link.txt").read_text(), "original")

    def test_source_mutation_detected(self):
        snap = self.snap()
        (self.repo / "main.txt").write_text("later mutation")
        with self.assertRaises(HarnessError):
            verify_unchanged(snap)

    def test_unchanged_source_accepted(self):
        verify_unchanged(self.snap())

    def test_wrong_repo_root_refused(self):
        child = self.repo / "app"
        child.mkdir()
        with self.assertRaises(HarnessError):
            source_files(child)

    def test_submodules_refused_explicitly(self):
        self.file("source/.gitmodules", "[submodule]")
        with self.assertRaises(HarnessError):
            self.snap()

    def test_relative_paths_cannot_escape(self):
        for value in ["../x", "/etc", "a/../../x"]:
            with self.subTest(value=value), self.assertRaises(HarnessError):
                safe_relative(value)
        self.assertEqual(safe_relative("apps/backend"), Path("apps/backend"))


class EvidenceTests(TempCase):
    def junit(self, tests=3, failures=0, errors=0, skipped=0):
        return self.file("test-results/TEST-example.xml", f'<testsuite tests="{tests}" failures="{failures}" errors="{errors}" skipped="{skipped}"/>').parent

    def test_junit_pass(self):
        self.assertEqual(require_junit(self.junit())["tests"], 3)

    def test_junit_absent_fails(self):
        with self.assertRaises(HarnessError):
            require_junit(self.root)

    def test_junit_zero_tests_fails(self):
        with self.assertRaises(HarnessError):
            require_junit(self.junit(tests=0))

    def test_junit_all_skipped_fails(self):
        with self.assertRaises(HarnessError):
            require_junit(self.junit(skipped=3))

    def test_junit_failure_fails(self):
        with self.assertRaises(HarnessError):
            require_junit(self.junit(failures=1))

    def test_junit_error_fails(self):
        with self.assertRaises(HarnessError):
            require_junit(self.junit(errors=1))

    def test_junit_wrapped_suites_counted(self):
        self.file("TEST-root.xml", '<testsuites><testsuite tests="2"/><testsuite tests="4"/></testsuites>')
        self.assertEqual(require_junit(self.root)["tests"], 6)

    def test_jacoco_threshold(self):
        path = self.file("coverage.xml", '<report><counter type="LINE" covered="70" missed="30"/><counter type="BRANCH" covered="6" missed="4"/></report>')
        self.assertEqual(jacoco_coverage(path, 70, 60)["lines"], 70)
        with self.assertRaises(HarnessError):
            jacoco_coverage(path, 71, 60)

    def test_jacoco_zero_lines_fails(self):
        path = self.file("coverage.xml", '<report><counter type="LINE" covered="0" missed="0"/></report>')
        with self.assertRaises(HarnessError):
            jacoco_coverage(path, 0, 0)

    def test_jacoco_no_branches_is_na(self):
        path = self.file("coverage.xml", '<report><counter type="LINE" covered="3" missed="0"/></report>')
        self.assertIsNone(jacoco_coverage(path, 70, 60)["branches"])

    def test_missing_jacoco_fails(self):
        with self.assertRaises(HarnessError):
            jacoco_coverage(self.root / "missing", 70, 60)

    def test_frontend_coverage_uses_counters_not_claimed_percent(self):
        path = self.json("coverage.json", {"total": {"lines": {"total": 10, "covered": 1, "pct": 100}, "branches": {"total": 0, "covered": 0}}})
        with self.assertRaises(HarnessError):
            frontend_coverage(path, 70, 60)

    def test_frontend_coverage_pass(self):
        path = self.json("coverage.json", {"total": {"lines": {"total": 10, "covered": 8}, "branches": {"total": 5, "covered": 4}}})
        self.assertEqual(frontend_coverage(path, 70, 60)["lines"], 80)

    def test_frontend_invalid_coverage_fails(self):
        path = self.json("coverage.json", {"total": {"lines": {"total": 0, "covered": 0}}})
        with self.assertRaises(HarnessError):
            frontend_coverage(path, 0, 0)

    def semgrep(self, results=None, errors=None, scanned=None):
        return self.json("semgrep.json", {"results": results or [], "errors": errors or [], "paths": {"scanned": ["src/A.java"] if scanned is None else scanned}})

    def test_semgrep_clean(self):
        self.assertEqual(validate_semgrep(self.semgrep())["_status"], "PASS")

    def test_semgrep_warning_visible(self):
        result = validate_semgrep(self.semgrep([{"extra": {"severity": "WARNING"}}]))
        self.assertEqual(result["_status"], "WARN")

    def test_semgrep_error_blocks(self):
        with self.assertRaises(HarnessError):
            validate_semgrep(self.semgrep([{"extra": {"severity": "ERROR"}}]))

    def test_semgrep_parser_errors_block(self):
        with self.assertRaises(HarnessError):
            validate_semgrep(self.semgrep(errors=[{"message": "parse failed"}]))

    def test_semgrep_no_scanned_files_blocks(self):
        with self.assertRaises(HarnessError):
            validate_semgrep(self.semgrep(scanned=[]))

    def test_trivy_zero_packages_blocks(self):
        with self.assertRaises(HarnessError):
            validate_trivy(self.json("trivy.json", {"Results": []}))

    def test_trivy_high_blocks(self):
        data = {"Results": [{"Packages": [{"Name": "test"}], "Vulnerabilities": [{"Severity": "HIGH"}]}]}
        with self.assertRaises(HarnessError):
            validate_trivy(self.json("trivy.json", data))

    def test_trivy_clean_packages_pass(self):
        data = {"Results": [{"Packages": [{"Name": "test"}], "Vulnerabilities": []}]}
        self.assertEqual(validate_trivy(self.json("trivy.json", data))["packages"], 1)

    def test_trivy_no_iac_inputs_allowed(self):
        self.assertEqual(validate_trivy(self.json("trivy.json", {}), False)["blocking_findings"], 0)


class MemoryTests(unittest.TestCase):
    def test_memory_values_are_converted_safely(self):
        self.assertEqual(memory_bytes("4g"), 4 * 1024 ** 3)
        self.assertEqual(memory_bytes("512MiB"), 512 * 1024 ** 2)
        self.assertEqual(format_memory(8 * 1024 ** 3), "8.0 GiB")

    def test_invalid_memory_values_are_rejected(self):
        for value in ("", "0g", "0.1b", "4", "4g; rm -rf /", "-1g"):
            with self.subTest(value=value), self.assertRaises(HarnessError):
                memory_bytes(value)


class RunnerTests(TempCase):
    def setUp(self):
        super().setUp()
        (self.root / "policy").mkdir()
        self.json("images.lock.json", {"images": {"node": {"digest": "node@sha256:" + "a" * 64}}})
        self.r = Runner(self.root, {}, "test", "frontend")
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_missing_image_pin_blocks(self):
        with self.assertRaises(HarnessError):
            self.r.image("unlocked")

    def test_blocked_means_failed_run(self):
        self.r.blocked("missing", "No coverage")
        self.assertEqual(self.r.finish(), 1)
        self.assertEqual(read_json(self.r.reports / "summary.json")["status"], "FAIL")

    def test_success_updates_latest_and_markdown(self):
        self.r.check("ok", lambda: {"executed": 1})
        self.assertEqual(self.r.finish(), 0)
        self.assertTrue((self.root / "reports/latest").is_symlink())
        self.assertIn("**PASS**", (self.r.reports / "summary.md").read_text())

    def test_namespaced_runner_keeps_projects_separate(self):
        runner = Runner(self.root, {}, "gate", "app", namespace="my-app",
                        metadata={"gate": {"status": "READY"}})
        runner.check("ok", lambda: {"executed": 1})
        self.assertEqual(runner.finish(), 0)
        summary = read_json(runner.reports / "summary.json")
        self.assertEqual(summary["gate"]["status"], "READY")
        self.assertTrue((self.root / "reports/my-app/latest").is_symlink())
        self.assertTrue(str(runner.reports).endswith("/reports/my-app/" + runner.id))

    def test_warning_is_explicit(self):
        self.r.check("review", lambda: {"_status": "WARN", "count": 2})
        self.assertEqual(self.r.results[0].status, "WARN")

    def test_bad_json_is_error(self):
        path = self.file("bad.json", "not JSON")
        self.assertFalse(self.r.check("bad", lambda: read_json(path)))

    def test_infrastructure_error_stays_distinct_from_app_failure(self):
        def runtime_failure():
            raise InfrastructureError("Docker runtime is too small")

        self.assertFalse(self.r.check("runtime", runtime_failure))
        self.assertEqual(self.r.results[-1].status, "ERROR")

    @patch("core.subprocess.Popen")
    def test_docker_token_not_in_argv_and_policy_readonly(self, popen):
        proc = popen.return_value
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        self.assertTrue(self.r.docker("node", "node", ["--version"], env={"SONAR_TOKEN": "not-a-real-token"}))
        command = popen.call_args.args[0]
        self.assertNotIn("not-a-real-token", command)
        self.assertEqual(popen.call_args.kwargs["env"]["SONAR_TOKEN"], "not-a-real-token")
        self.assertTrue(any("dst=/policy,readonly" in arg for arg in command))
        self.assertFalse(any("docker.sock" in arg for arg in command))
        self.assertIn("--read-only", command)

    @patch("core.subprocess.Popen")
    def test_docker_accepts_separate_stage_memory_limit(self, popen):
        popen.return_value.wait.return_value = 0
        popen.return_value.poll.return_value = 0
        self.assertTrue(self.r.docker("sonar", "node", [], memory="6g"))
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--memory") + 1], "6g")

    @patch("core.subprocess.Popen")
    def test_docker_rejects_invalid_memory_limit_before_start(self, popen):
        with self.assertRaises(HarnessError):
            self.r.docker("sonar", "node", [], memory="4g; unsafe")
        popen.assert_not_called()

    @patch("core.subprocess.Popen")
    def test_docker_nonzero_never_passes(self, popen):
        popen.return_value.wait.return_value = 1
        popen.return_value.poll.return_value = 1
        self.assertFalse(self.r.docker("bad", "node", ["bad"]))
        self.assertEqual(self.r.finish(), 1)

    @patch("core.subprocess.Popen")
    def test_docker_infrastructure_error_distinct(self, popen):
        popen.return_value.wait.return_value = 125
        popen.return_value.poll.return_value = 125
        self.r.docker("infra", "node", [])
        self.assertEqual(self.r.results[-1].status, "ERROR")

    @patch("core.subprocess.run")
    @patch("core.subprocess.Popen")
    def test_docker_timeout_cleans_up(self, popen, run):
        popen.return_value.wait.side_effect = [subprocess.TimeoutExpired("docker", 1), 0]
        popen.return_value.poll.return_value = None
        self.assertFalse(self.r.docker("timeout", "node", []))
        self.assertEqual(self.r.results[-1].status, "ERROR")
        self.assertEqual(run.call_args.args[0][:3], ["docker", "rm", "-f"])

    @patch("core.subprocess.Popen")
    def test_zap_warning_exit_is_not_hidden(self, popen):
        popen.return_value.wait.return_value = 2
        popen.return_value.poll.return_value = 2
        self.assertTrue(self.r.docker("zap", "node", [], allowed_codes=(0, 2)))
        self.assertEqual(self.r.results[-1].status, "WARN")


if __name__ == "__main__":
    unittest.main()
