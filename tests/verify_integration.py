"""Explicit Docker integration checks. No mocks, automatic skips or installations.

Run from the harness checkout: python3 tests/verify_integration.py
This exercises the custom adapter with Node's real tests, coverage and local scans.
It does not certify Next.js, Maven, Playwright or Sonar integration.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
from core import HarnessError, git, read_json, write_json
from project import project_slug


def main() -> int:
    session = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    workspace = ROOT / ".local/integration" / session
    repo = workspace / "native-node"
    shutil.copytree(ROOT / "tests/fixtures/native-node", repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Harness Integration")
    git(repo, "config", "user.email", "integration@example.invalid")
    profile = {
        "schema_version": 2, "project": {"name": "integration-" + session, "adapter": "custom", "subdir": "."},
        "target_branch": "main", "stages": ["secrets", "static-analysis", "tests", "coverage", "build"],
        "commands": {"test": ["node", "--test", "tests/classify.test.js"],
                     "coverage": ["node", "tools/coverage.js"], "build": ["node", "--check", "src/classify.js"]},
        "test_reporter": "node-junit", "environment": {}, "thresholds": {"lines": 70, "branches": 60},
        "evidence": {"tests": "custom-reports", "coverage": "coverage/coverage-summary.json", "lcov": "coverage/lcov.info"},
    }
    write_json(repo / ".ci/harness.json", profile)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "native Node integration fixture")
    runs = []

    def gate(case: str, expected: str, failing_step: str | None = None) -> dict:
        logfile = workspace / f"{case}.log"
        with logfile.open("w") as output:
            process = subprocess.run([str(ROOT / "ci"), "gate", "--repo", str(repo), "--intent", "push-main"],
                                     stdout=output, stderr=subprocess.STDOUT, timeout=600, check=False)
        summary_path = ROOT / "reports" / project_slug(profile, repo) / "latest/summary.json"
        if not summary_path.exists():
            raise HarnessError(f"{case}: no gate report; infrastructure failure; see {logfile}")
        report = read_json(summary_path)
        status = report.get("gate", {}).get("status")
        if any(step["status"] == "ERROR" for step in report["steps"]) or process.returncode == 2:
            raise HarnessError(f"{case}: infrastructure ERROR; see {summary_path} and {logfile}")
        expected_code = 0 if expected == "READY" else 1
        if status != expected or process.returncode != expected_code:
            raise HarnessError(f"{case}: expected {expected}/{expected_code}, got {status}/{process.returncode}; see {summary_path}")
        if failing_step and not any(s["name"] == failing_step and s["status"] in ("FAIL", "BLOCKED", "WARN") for s in report["steps"]):
            raise HarnessError(f"{case}: expected finding at {failing_step}; see {summary_path}")
        runs.append({"case": case, "status": status, "exit_code": process.returncode,
                     "run_id": report["run_id"], "report": str(summary_path.resolve()), "log": str(logfile)})
        print(f"{case}: {status}, run {report['run_id']}", flush=True)
        return report

    try:
        first = gate("pass", "READY")
        repeated = gate("repeat", "READY")
        for key in ("source_hash", "profile_sha256", "policy_sha256", "dependency_manifests"):
            if first[key] != repeated[key]:
                raise HarnessError(f"Repeated gate changed {key}")
        if [(s["name"], s["status"]) for s in first["steps"]] != [(s["name"], s["status"]) for s in repeated["steps"]]:
            raise HarnessError("Repeated gate changed check results")
        profile["commands"]["test"] = ["node", "--version"]
        write_json(repo / ".ci/harness.json", profile)
        stale = repo / "custom-reports/TEST-old.xml"
        stale.parent.mkdir(exist_ok=True)
        stale.write_text('<testsuite tests="99" failures="0" errors="0" skipped="0"/>')
        gate("stale-report", "FAIL", "project-tests-evidence")
        if not stale.exists():
            raise HarnessError("Gate modified the original fixture report")
        stale.unlink()
        profile["commands"]["test"] = ["node", "--test", "tests/classify.test.js"]
        write_json(repo / ".ci/harness.json", profile)
        tests = repo / "tests/classify.test.js"
        original_tests = tests.read_text()
        tests.write_text(original_tests + '\ntest.skip("visible skip", () => {});\n')
        gate("partial-skip", "REVIEW", "project-tests-evidence")
        tests.write_text(original_tests)
        source = repo / "src/classify.js"
        source.write_text(source.read_text().replace('return "negative"', 'return "positive"'))
        gate("caught-mutation", "FAIL", "project-tests")
        profile["stages"] = ["build"]
        write_json(repo / ".ci/harness.json", profile)
        gate("incomplete-profile", "BLOCKED", "project-quality-policy")
    except (HarnessError, OSError, subprocess.TimeoutExpired) as exc:
        write_json(workspace / "verification.json", {"status": "FAIL", "error": str(exc), "runs": runs})
        print(str(exc), file=sys.stderr)
        return 1
    write_json(workspace / "verification.json", {"status": "PASS", "runs": runs,
                                                "scope": "custom Node adapter; no Next.js, Maven, browser or Sonar"})
    print(f"Integration evidence: {workspace / 'verification.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
