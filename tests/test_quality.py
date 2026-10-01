"""Regression tests for complete gates and evidence from the current step."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
from core import HarnessError, Result, Runner, git, read_json, require_junit, snapshot, write_json
from quality import load_quality_policy, project_quality_issues
import main


class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "app"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Quality Tests")
        git(self.repo, "config", "user.email", "quality@example.invalid")
        (self.repo / "app.py").write_text("answer = 42\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "initial")
        (self.root / "policy").mkdir()
        write_json(self.root / "images.lock.json", {"images": {
            "python": {"digest": "python@sha256:" + "a" * 64, "os": "linux", "architecture": "arm64"}
        }})
        self.profile = {
            "schema_version": 2,
            "project": {"name": "app", "adapter": "custom", "subdir": "."},
            "target_branch": "main", "stages": ["build"],
            "commands": {"build": ["python", "-m", "compileall", "."]},
            "evidence": {}, "thresholds": {"lines": 70, "branches": 60},
            "environment": {},
        }
        # Installation policy is an independent input, never a project override.
        write_json(self.root / "policy/quality.json", {
            "schema_version": 1,
            "required_stages": ["secrets", "static-analysis", "tests", "coverage", "build"],
            "adapter_required_stages": {"next-npm": ["dependencies", "lint-typecheck"],
                "next-fullstack": ["dependencies", "lint-typecheck"], "spring-maven": ["dependencies"],
                "generic": [], "custom": []},
            "coverage_min": {"lines": 70, "branches": 60},
            "execution_environment": {"TZ": "UTC", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONHASHSEED": "0"},
        })

    def gate(self):
        write_json(self.repo / ".ci/harness.json", self.profile)
        with patch.object(main, "ROOT", self.root), patch("main.policy_hash", return_value="policy"), \
             patch("main.run_profile_command", return_value=True), contextlib.redirect_stdout(io.StringIO()):
            code = main.run_profile_pipeline({}, self.profile, self.repo, "push-main")
        return code, read_json(self.root / "reports/app/latest/summary.json")

    def test_build_only_cannot_be_ready(self):
        code, report = self.gate()
        self.assertNotEqual(code, 0)
        self.assertEqual(report["gate"]["status"], "BLOCKED")
        self.assertIn("tests", json.dumps(report["steps"]))

    def test_zero_coverage_thresholds_cannot_be_ready(self):
        self.profile["thresholds"] = {"lines": 0, "branches": 0}
        code, report = self.gate()
        self.assertNotEqual(code, 0)
        self.assertIn("coverage", json.dumps(report["steps"]).lower())

    def test_old_custom_junit_cannot_satisfy_successful_noop_command(self):
        old = self.repo / "custom-reports/TEST-old.xml"
        old.parent.mkdir()
        old.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>')
        snap = snapshot(self.repo, self.root / "copy", ".", False)
        runner = Runner(self.root, {}, "test", "app")
        profile = dict(self.profile, commands={"test": ["python", "--version"]},
                       evidence={"tests": "custom-reports"})
        with patch.object(main, "ROOT", self.root), patch.object(runner, "docker", return_value=True):
            self.assertTrue(main.run_profile_command(runner, profile, snap, "project-tests", "test"))
        self.assertFalse((snap.app / "custom-reports/TEST-old.xml").exists())
        self.assertTrue(old.exists(), "the original repository must stay untouched")

    def test_quality_requirements_cannot_be_bypassed_by_generic_adapter(self):
        profile = dict(self.profile, project=dict(self.profile["project"], adapter="generic"))
        issues = project_quality_issues(profile, load_quality_policy(self.root), self.repo)
        self.assertTrue(any("tests" in issue and "coverage" in issue for issue in issues))

    def test_dependency_manifest_requires_scan_even_for_custom_adapter(self):
        (self.repo / "requirements.txt").write_text("example==1.0\n")
        self.profile["stages"] = ["secrets", "static-analysis", "tests", "coverage", "build"]
        issues = project_quality_issues(self.profile, load_quality_policy(self.root), self.repo)
        self.assertEqual(issues, ["Missing centrally required stages: dependencies"])

    def test_stronger_complete_profile_is_accepted(self):
        self.profile["stages"] = ["secrets", "static-analysis", "tests", "coverage", "build"]
        self.profile["thresholds"] = {"lines": 90, "branches": 85}
        self.assertEqual(project_quality_issues(self.profile, load_quality_policy(self.root), self.repo), [])

    def test_missing_quality_policy_blocks_with_report(self):
        (self.root / "policy/quality.json").unlink()
        code, report = self.gate()
        self.assertNotEqual(code, 0)
        self.assertEqual(report["gate"]["status"], "BLOCKED")
        self.assertIn("Cannot read JSON", report["steps"][0]["details"])

    def test_project_cannot_override_deterministic_environment(self):
        self.profile["environment"] = {"TZ": "Europe/Berlin"}
        issues = project_quality_issues(self.profile, load_quality_policy(self.root), self.repo)
        self.assertTrue(any("TZ=UTC" in issue for issue in issues))

    def test_old_coverage_and_lcov_are_removed_only_from_snapshot(self):
        self.profile["evidence"] = {"coverage": "custom-output/summary.json", "lcov": "custom-output/lcov.info"}
        for name in ("summary.json", "lcov.info"):
            path = self.repo / "custom-output" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("stale")
        snap = snapshot(self.repo, self.root / "copy", ".", False)
        main.prepare_profile_evidence(self.profile, snap, "coverage")
        self.assertFalse((snap.app / "custom-output/summary.json").exists())
        self.assertFalse((snap.app / "custom-output/lcov.info").exists())
        self.assertTrue((self.repo / "custom-output/summary.json").exists())

    def test_evidence_symlink_is_blocked_before_any_deletion(self):
        snap = snapshot(self.repo, self.root / "copy", ".", False)
        reports = snap.app / "test-results"
        reports.mkdir()
        external = self.root / "external.xml"
        external.write_text("unchanged")
        (reports / "TEST-link.xml").symlink_to(external)
        with self.assertRaises(HarnessError):
            main.prepare_profile_evidence(self.profile, snap, "test")
        self.assertEqual(external.read_text(), "unchanged")

    def test_stage_archives_survive_shared_evidence_directory_cleanup(self):
        snap = snapshot(self.repo, self.root / "copy", ".", False)
        runner = Runner(self.root, {}, "test", "app")
        reports = snap.app / "test-results"
        reports.mkdir()
        (reports / "TEST-unit.xml").write_text('<testsuite tests="2"/>')
        runner.results.append(Result("project-tests", "PASS", 0))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(main.check_profile_evidence(runner, self.profile, snap, "unit-evidence", "tests",
                                                        "test-results", require_junit, "project-tests"))
        main.prepare_profile_evidence(self.profile, snap, "e2e")
        self.assertFalse((reports / "TEST-unit.xml").exists())
        proof = runner.meta["evidence"]["unit-evidence"]
        self.assertEqual(proof["run_id"], runner.id)
        self.assertEqual(proof["producer"], "project-tests")
        self.assertTrue((runner.reports / proof["files"][0]["artifact"]).exists())
        self.assertEqual(len(proof["files"][0]["sha256"]), 64)

    def test_runs_cannot_share_writable_caches(self):
        first = Runner(self.root, {}, "test", "first")
        second = Runner(self.root, {}, "test", "second")
        self.assertNotEqual(main.cache("npm", first), main.cache("npm", second))
        (main.cache("npm", first) / "marker").write_text("first")
        self.assertFalse((main.cache("npm", second) / "marker").exists())

    def test_archive_cannot_follow_job_created_symlink(self):
        snap = snapshot(self.repo, self.root / "copy", ".", False)
        runner = Runner(self.root, {}, "test", "app")
        reports = snap.app / "test-results"
        reports.mkdir()
        (reports / "TEST-unit.xml").write_text('<testsuite tests="1"/>')
        external = self.root / "outside"
        external.mkdir()
        (runner.reports / "project").symlink_to(external)
        runner.results.append(Result("project-tests", "PASS", 0))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(main.check_profile_evidence(runner, self.profile, snap, "unit", "tests",
                                                        "test-results", require_junit, "project-tests"))
        self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
