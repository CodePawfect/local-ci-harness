import json
import sys
import tempfile
import unittest
from unittest.mock import call, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))

from core import HarnessError, Result, git, read_json, write_json
from main import _gate_exit_code, _gate_status, gate_project
from project import (
    Detection,
    branch_context,
    detect_project,
    load_profile,
    profile_from_detection,
    profile_issues,
    project_slug,
    render_agent_prompt,
    validate_profile,
    write_agent_prompt,
    write_profile,
)
from tui import _session, format_detection


class ProjectProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "app"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Profile Tests")
        git(self.repo, "config", "user.email", "profile@example.invalid")

    def tearDown(self):
        self.temp.cleanup()

    def commit(self):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "initial")

    def test_detects_next_fullstack_and_commands(self):
        write_json(self.repo / "package.json", {
            "dependencies": {"next": "16.0.0"},
            "scripts": {
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
                "test": "node --test",
                "build": "next build",
                "test:e2e": "playwright test",
            },
        })
        write_json(self.repo / "package-lock.json", {"lockfileVersion": 3})
        (self.repo / "playwright.config.ts").write_text("export default {}")
        (self.repo / "src").mkdir()
        self.commit()
        detection = detect_project(self.repo)
        self.assertEqual(detection.adapter, "next-fullstack")
        self.assertIn("test", detection.commands)
        self.assertIn("e2e", detection.stages)
        self.assertEqual(detection.target_branch, "main")

    def test_detects_node_test_runner_reporter(self):
        write_json(self.repo / "package.json", {
            "dependencies": {"next": "16.0.0"},
            "scripts": {"test": "node --test", "test:coverage": "node --test"},
        })
        write_json(self.repo / "package-lock.json", {"lockfileVersion": 3})
        self.commit()
        self.assertEqual(detect_project(self.repo).test_reporter, "node-junit")

    def test_generic_detection_does_not_claim_dependency_scan_without_manifest(self):
        (self.repo / "README.md").write_text("plain repository")
        self.commit()
        detection = detect_project(self.repo)
        self.assertEqual(detection.adapter, "generic")
        self.assertNotIn("dependencies", detection.stages)

    def test_unsupported_lockfile_is_a_prompt_visible_hint(self):
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: 9")
        self.commit()
        detection = detect_project(self.repo)
        self.assertEqual(detection.adapter, "generic")
        profile = profile_from_detection(detection)
        self.assertTrue(any("pnpm" in note for note in profile["notes"]))
        prompt = render_agent_prompt(profile, self.repo, Path("/tmp/ci"), Path("/tmp/reports"))
        self.assertIn("pnpm-lock.yaml", prompt)

    def test_profile_rejects_shell_commands_and_policy_overrides(self):
        profile = {
            "schema_version": 2,
            "project": {"name": "app", "adapter": "custom", "subdir": "."},
            "target_branch": "main",
            "stages": ["secrets"],
            "commands": {"test": ["sh", "-c", "echo unsafe"]},
            "evidence": {},
            "thresholds": {"lines": 70, "branches": 60},
            "environment": {},
        }
        with self.assertRaises(HarnessError):
            validate_profile(profile, self.repo)
        profile["commands"] = {}
        profile["policy"] = {"allow": "everything"}
        with self.assertRaises(HarnessError):
            validate_profile(profile, self.repo)

    def test_custom_selected_test_without_command_is_visible(self):
        profile = {
            "schema_version": 2,
            "project": {"name": "app", "adapter": "custom", "subdir": "."},
            "target_branch": "main",
            "stages": ["tests"],
            "commands": {},
            "evidence": {},
            "thresholds": {"lines": 70, "branches": 60},
            "environment": {},
        }
        validate_profile(profile, self.repo)
        issues = profile_issues(profile)
        self.assertTrue(any("missing explicit command" in item for item in issues))
        self.assertEqual(project_slug(profile, self.repo), "app")

    def test_profile_and_prompt_are_written_under_ci(self):
        detection = type("Detection", (), {
            "repo": self.repo,
            "name": "app",
            "kind": "custom",
            "adapter": "generic",
            "target_branch": "main",
            "stages": ["secrets"],
            "commands": {},
            "evidence": {},
            "available_files": [],
            "available_scripts": [],
        })()
        profile = profile_from_detection(detection)
        profile_file = write_profile(self.repo, profile)
        prompt_file = write_agent_prompt(self.repo, profile, Path("/tmp/local-ci-harness/ci"),
                                         Path("/tmp/local-ci-harness/reports"))
        self.assertEqual(profile_file, self.repo / ".ci/harness.json")
        self.assertEqual(load_profile(self.repo)[0]["schema_version"], 2)
        self.assertIn("merge-to-main", prompt_file.read_text())
        self.assertIn("app/latest/summary.json", prompt_file.read_text())

    def test_branch_context_enforces_gate_intent(self):
        (self.repo / "README.md").write_text("initial")
        self.commit()
        git(self.repo, "checkout", "-qb", "feature/test")
        with self.assertRaises(HarnessError):
            branch_context(self.repo, "main", "push-main")
        context = branch_context(self.repo, "main", "merge-to-main")
        self.assertEqual(context["current_branch"], "feature/test")
        git(self.repo, "checkout", "-q", "main")
        with self.assertRaises(HarnessError):
            branch_context(self.repo, "main", "merge-to-main")
        self.assertEqual(branch_context(self.repo, "main", "push-main")["target_branch"], "main")

    def test_rejects_external_application_subdir_and_detached_gate(self):
        (self.repo / "README.md").write_text("initial")
        self.commit()
        outside = self.root / "outside"
        outside.mkdir()
        (self.repo / "linked-app").symlink_to(outside, target_is_directory=True)
        profile = {
            "schema_version": 2,
            "project": {"name": "app", "adapter": "generic", "subdir": "linked-app"},
            "target_branch": "main",
            "stages": ["secrets"],
            "commands": {},
            "evidence": {},
            "thresholds": {"lines": 70, "branches": 60},
            "environment": {},
        }
        with self.assertRaises(HarnessError):
            validate_profile(profile, self.repo)
        git(self.repo, "checkout", "--detach", "-q", "HEAD")
        with self.assertRaises(HarnessError):
            branch_context(self.repo, "main", "merge-to-main")

    def test_setup_tui_state_machine_can_run_without_terminal(self):
        detection = Detection(self.repo, "app", "custom", "generic", "main", ["secrets"], {}, {}, [], [])

        class FakeScreen:
            def __init__(self):
                self.keys = [10, 10, 10, ord("y"), ord("n")]

            def erase(self):
                pass

            def getmaxyx(self):
                return (40, 120)

            def addnstr(self, *args, **kwargs):
                pass

            def refresh(self):
                pass

            def getch(self):
                return self.keys.pop(0)

        profile, run_now = _session(FakeScreen(), detection)
        self.assertEqual(profile["project"]["adapter"], "generic")
        self.assertFalse(run_now)
        self.assertIn("adapter=generic", format_detection(detection))

    def test_gate_status_precedence_is_explicit(self):
        class FakeRunner:
            def __init__(self, statuses):
                self.results = [Result(name, status, 0) for name, status in enumerate(statuses)]

        self.assertEqual(_gate_status(FakeRunner(["PASS"])), "READY")
        self.assertEqual(_gate_status(FakeRunner(["PASS", "WARN"])), "REVIEW")
        self.assertEqual(_gate_status(FakeRunner(["FAIL", "WARN"])), "FAIL")
        self.assertEqual(_gate_status(FakeRunner(["ERROR", "BLOCKED"])), "BLOCKED")
        self.assertEqual(_gate_exit_code(FakeRunner(["PASS"]), "READY", 0), 0)
        self.assertEqual(_gate_exit_code(FakeRunner(["BLOCKED"]), "BLOCKED", 1), 1)
        self.assertEqual(_gate_exit_code(FakeRunner(["ERROR"]), "FAIL", 1), 2)

    def test_sonar_services_are_started_and_stopped_around_profile_gate(self):
        profile = {"stages": ["secrets", "sonar"]}
        def run_pipeline(*_args, **kwargs):
            self.assertIn("start_sonar", kwargs)
            kwargs["start_sonar"]()
            return 0

        with patch("main.repository_root", return_value=self.repo), \
             patch("main.load_profile", return_value=(profile, self.repo / ".ci" / "harness.json")), \
             patch("main.load_install_config", return_value={}), \
             patch("main.ensure_docker"), patch("main.ensure_network"), \
             patch("main.compose") as compose, patch("main.wait_ready"), \
             patch("main.sonar_url", return_value="http://127.0.0.1:9000"), \
             patch("main.run_profile_pipeline", side_effect=run_pipeline):
            self.assertEqual(gate_project("ignored", "merge-to-main"), 0)
        self.assertEqual(compose.call_args_list, [call("up", "-d"), call("down", "--remove-orphans")])


if __name__ == "__main__":
    unittest.main()
