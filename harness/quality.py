"""Trusted quality requirements; project profiles can only strengthen them."""
from __future__ import annotations

from pathlib import Path

from core import DEFAULT_EXECUTION_ENV, HarnessError, read_json
from project import ADAPTERS, STAGES


DEPENDENCY_MARKERS = {
    "package.json", "package-lock.json", "pom.xml", "requirements.txt", "pyproject.toml",
    "poetry.lock", "go.mod", "Cargo.toml", "Gemfile.lock", "composer.lock",
}


def load_quality_policy(root: Path) -> dict:
    policy = read_json(root / "policy/quality.json")
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise HarnessError("Unsupported central quality policy")
    required = policy.get("required_stages")
    adapters = policy.get("adapter_required_stages")
    if not isinstance(required, list) or not required or any(s not in STAGES for s in required):
        raise HarnessError("Central quality policy has invalid required_stages")
    if not isinstance(adapters, dict) or set(adapters) != ADAPTERS:
        raise HarnessError("Central quality policy must cover every adapter")
    for stages in adapters.values():
        if not isinstance(stages, list) or any(s not in STAGES for s in stages):
            raise HarnessError("Invalid central adapter requirements")
    minimums = policy.get("coverage_min")
    if not isinstance(minimums, dict):
        raise HarnessError("Central quality policy has no coverage minimums")
    for key in ("lines", "branches"):
        value = minimums.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100:
            raise HarnessError(f"Invalid central coverage minimum: {key}")
    if policy.get("execution_environment") != DEFAULT_EXECUTION_ENV:
        raise HarnessError("Central execution_environment must match the supported deterministic runtime defaults")
    return policy


def project_quality_issues(profile: dict, policy: dict, app: Path) -> list[str]:
    required = set(policy["required_stages"])
    required.update(policy["adapter_required_stages"][profile["project"]["adapter"]])
    if any((app / name).is_file() for name in DEPENDENCY_MARKERS):
        required.add("dependencies")
    missing = sorted(required - set(profile["stages"]))
    issues = ["Missing centrally required stages: " + ", ".join(missing)] if missing else []
    thresholds = profile.get("thresholds", {})
    for key, minimum in policy["coverage_min"].items():
        value = thresholds.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= 100:
            issues.append(f"coverage {key} must be at least the central minimum {minimum}%")
    for key, value in DEFAULT_EXECUTION_ENV.items():
        if key in profile.get("environment", {}) and profile["environment"][key] != value:
            issues.append(f"Project environment cannot override central {key}={value}")
    return issues
