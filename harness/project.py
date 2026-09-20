"""Project profiles, repository detection and merge-intent metadata.

The project profile is deliberately separate from the harness installation
configuration. Project files are input data: they can select an adapter and
commands, but they cannot replace the central policy or image lock.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

from core import HarnessError, git, read_json, safe_relative, write_json


PROFILE_SCHEMA_VERSION = 2
ADAPTERS = {"generic", "next-npm", "next-fullstack", "spring-maven", "custom"}
STAGES = (
    "secrets",
    "static-analysis",
    "dependencies",
    "lint-typecheck",
    "tests",
    "integration",
    "coverage",
    "build",
    "e2e",
    "zap",
    "sonar",
)
COMMAND_KEYS = {"install", "lint", "typecheck", "test", "coverage", "format", "build", "e2e", "integration"}
SHELL_PROGRAMS = {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"}


@dataclasses.dataclass(frozen=True)
class Detection:
    repo: Path
    name: str
    kind: str
    adapter: str
    target_branch: str
    stages: list[str]
    commands: dict[str, list[str]]
    evidence: dict[str, str]
    available_files: list[str]
    available_scripts: list[str]
    test_reporter: str | None = None
    hints: list[str] = dataclasses.field(default_factory=list)


def repository_root(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_dir():
        raise HarnessError(f"Repository directory not found: {candidate}")
    return Path(git(candidate, "rev-parse", "--show-toplevel")).resolve()


def profile_path(repo: Path) -> Path:
    return repo / ".ci" / "harness.json"


def prompt_path(repo: Path) -> Path:
    return repo / ".ci" / "agent-prompt.md"


def project_slug(profile: dict[str, Any], repo: Path) -> str:
    name = str(profile.get("project", {}).get("name") or repo.name).lower()
    value = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    if not value:
        raise HarnessError("Project name must contain at least one alphanumeric character")
    return value[:64]


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise HarnessError(f"Cannot hash project profile {path}: {exc}") from exc


def current_branch(repo: Path) -> str:
    try:
        return git(repo, "symbolic-ref", "--quiet", "--short", "HEAD") or "DETACHED"
    except HarnessError:
        return "DETACHED"


def _ref_exists(repo: Path, ref: str) -> bool:
    result = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", ref], check=False)
    return result.returncode == 0


def detect_target_branch(repo: Path) -> str:
    try:
        remote_head = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        if remote_head.startswith("origin/"):
            return remote_head.removeprefix("origin/")
    except HarnessError:
        pass
    for candidate in ("main", "master"):
        if _ref_exists(repo, f"refs/heads/{candidate}") or _ref_exists(repo, f"refs/remotes/origin/{candidate}"):
            return candidate
    return "main"


def branch_context(repo: Path, target_branch: str, intent: str) -> dict[str, Any]:
    if intent not in ("merge-to-main", "push-main"):
        raise HarnessError(f"Unsupported gate intent: {intent}")
    branch = current_branch(repo)
    if branch == "DETACHED":
        raise HarnessError("Gate runs require a named source/target branch; detached HEAD is not accepted")
    target_ref = target_branch
    if not _ref_exists(repo, f"refs/heads/{target_branch}"):
        if _ref_exists(repo, f"refs/remotes/origin/{target_branch}"):
            target_ref = f"origin/{target_branch}"
        else:
            raise HarnessError(f"Target branch does not exist locally or under origin: {target_branch}")
    if intent == "merge-to-main" and branch == target_branch:
        raise HarnessError(f"merge-to-main requires a non-target branch; current branch is {branch}")
    if intent == "push-main" and branch != target_branch:
        raise HarnessError(f"push-main requires branch {target_branch}; current branch is {branch}")
    try:
        merge_base = git(repo, "merge-base", "HEAD", target_ref)
    except HarnessError as exc:
        raise HarnessError(f"Cannot determine merge base against {target_ref}: {exc}") from exc
    return {
        "current_branch": branch,
        "target_branch": target_branch,
        "target_ref": target_ref,
        "head_commit": git(repo, "rev-parse", "HEAD"),
        "merge_base": merge_base,
        "intent": intent,
    }


def _package_scripts(repo: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    package_path = repo / "package.json"
    lock_path = repo / "package-lock.json"
    package = read_json(package_path) if package_path.exists() else {}
    lock = read_json(lock_path) if lock_path.exists() else {}
    return package, lock


def _npm_commands(scripts: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}

    def add(key: str, *names: str) -> None:
        for name in names:
            if name in scripts:
                result[key] = ["npm", "run", name]
                return

    add("lint", "lint")
    add("typecheck", "typecheck")
    add("test", "test:ci", "test:unit", "test")
    add("coverage", "test:coverage", "coverage")
    add("format", "format:check")
    add("build", "build")
    add("e2e", "test:e2e", "e2e")
    add("integration", "test:integration", "test:rls", "test:security:db")
    return result


def detect_project(repo: Path) -> Detection:
    repo = repository_root(repo)
    package, lock = _package_scripts(repo)
    scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
    dependencies: dict[str, Any] = {}
    if isinstance(package, dict):
        dependencies.update(package.get("dependencies", {}))
        dependencies.update(package.get("devDependencies", {}))
    commands = _npm_commands(scripts if isinstance(scripts, dict) else {})
    test_reporter = None
    test_command = ""
    if isinstance(scripts, dict):
        for script_name in ("test:ci", "test:unit", "test"):
            if script_name in scripts:
                test_command = scripts[script_name]
                break
    if isinstance(test_command, str) and "--test" in test_command and "node" in test_command:
        test_reporter = "node-junit"
    files = {p.name for p in repo.iterdir() if p.is_file()}
    has_next = "next" in dependencies
    pom = repo / "pom.xml"
    spring = pom.exists() and "spring-boot" in pom.read_text(encoding="utf-8", errors="replace").lower()
    has_playwright = any((repo / name).exists() for name in ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs"))
    hints: list[str] = []
    if (repo / "pnpm-lock.yaml").exists():
        hints.append("pnpm-lock.yaml detected: use a custom adapter; the V1 runtime is npm-only")
    if (repo / "yarn.lock").exists():
        hints.append("yarn.lock detected: use a custom adapter; the V1 runtime is npm-only")
    pom_text = pom.read_text(encoding="utf-8", errors="replace") if pom.exists() else ""
    if "<modules>" in pom_text:
        hints.append("Maven multi-module project detected: V1 requires explicit aggregate evidence and refuses automatic reactor handling")
    if has_next:
        adapter, kind = "next-fullstack", "next-fullstack"
        stages = ["secrets", "static-analysis", "dependencies", "lint-typecheck", "tests", "build"]
        if "coverage" in commands:
            stages.insert(stages.index("build"), "coverage")
        if has_playwright or "e2e" in commands:
            stages.append("e2e")
        evidence = {"tests": "test-results", "coverage": "coverage/coverage-summary.json", "lcov": "coverage/lcov.info"}
        if "e2e" in stages:
            evidence["e2e"] = "test-results"
    elif spring:
        adapter, kind = "spring-maven", "backend"
        stages = ["secrets", "static-analysis", "dependencies", "tests", "coverage", "build"]
        maven_args = ["-Pci"]
        commands = {
            "install": ["mvn", "-B", "-ntp", *maven_args],
            "test": ["mvn", "-B", "-ntp", *maven_args, "test"],
            "build": ["mvn", "-B", "-ntp", *maven_args, "clean", "verify"],
        }
        evidence = {
            "tests": "target/surefire-reports",
            "integration": "target/failsafe-reports",
            "coverage": "target/site/jacoco/jacoco.xml",
            "sbom": "target/bom.json",
        }
    elif package or lock:
        adapter, kind = "custom", "frontend"
        stages = ["secrets", "static-analysis", "dependencies"]
        evidence = {"tests": "test-results", "coverage": "coverage/coverage-summary.json", "lcov": "coverage/lcov.info"}
    else:
        adapter, kind = "generic", "custom"
        dependency_markers = {
            "pom.xml", "requirements.txt", "pyproject.toml", "poetry.lock", "go.mod", "go.sum",
            "Cargo.toml", "Cargo.lock", "Gemfile.lock", "composer.lock", "Dockerfile",
        }
        stages = ["secrets", "static-analysis"]
        if files.intersection(dependency_markers):
            stages.append("dependencies")
        evidence = {}
    return Detection(
        repo,
        repo.name,
        kind,
        adapter,
        detect_target_branch(repo),
        stages,
        commands,
        evidence,
        sorted(files),
        sorted(scripts) if isinstance(scripts, dict) else [],
        test_reporter,
        hints,
    )


def profile_from_detection(detection: Detection, adapter: str | None = None,
                           stages: list[str] | None = None, kind: str | None = None) -> dict[str, Any]:
    selected_adapter = adapter or detection.adapter
    if selected_adapter not in ADAPTERS:
        raise HarnessError(f"Unsupported adapter: {selected_adapter}")
    chosen_stages = list(stages if stages is not None else detection.stages)
    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "project": {
            "name": detection.name,
            "kind": kind or detection.kind,
            "adapter": selected_adapter,
            "subdir": ".",
        },
        "target_branch": detection.target_branch,
        "stages": chosen_stages,
        "commands": detection.commands,
        "evidence": detection.evidence,
        "thresholds": {"lines": 70, "branches": 60},
        "environment": {},
        "sonar_key": detection.name.lower().replace(" ", "-")[:64],
    }
    if selected_adapter == "spring-maven":
        profile["maven_args"] = ["-Pci"]
    if getattr(detection, "test_reporter", None):
        profile["test_reporter"] = detection.test_reporter
    profile["notes"] = list(getattr(detection, "hints", []))
    validate_profile(profile, detection.repo)
    return profile


def _validate_command(key: str, command: Any) -> None:
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise HarnessError(f"commands.{key} must be a non-empty argument array")
    if any("\x00" in item for item in command):
        raise HarnessError(f"commands.{key} contains a NUL byte")
    if command[0].lower() in SHELL_PROGRAMS or any(item in ("-c", "-lc", "/c") for item in command):
        raise HarnessError(f"commands.{key} may not invoke a shell; use explicit argv tokens")


def validate_profile(profile: dict[str, Any], repo: Path | None = None) -> dict[str, Any]:
    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise HarnessError(f"Unsupported project profile schema: {profile.get('schema_version')}")
    project = profile.get("project")
    if not isinstance(project, dict):
        raise HarnessError("Project profile requires a project object")
    for key in ("name", "adapter", "subdir"):
        if not isinstance(project.get(key), str) or not project[key]:
            raise HarnessError(f"Missing project.{key}")
    if project.get("kind") is not None and project["kind"] not in {"frontend", "backend", "next-fullstack", "custom"}:
        raise HarnessError("project.kind must be frontend, backend, next-fullstack or custom")
    if project["adapter"] not in ADAPTERS:
        raise HarnessError(f"Unsupported project adapter: {project['adapter']}")
    safe_relative(project["subdir"])
    target = profile.get("target_branch")
    if not isinstance(target, str) or not target or ".." in Path(target).parts or not re.fullmatch(r"[A-Za-z0-9._/-]+", target):
        raise HarnessError("target_branch must be a safe Git branch name")
    stages = profile.get("stages")
    if not isinstance(stages, list) or not stages or any(stage not in STAGES for stage in stages):
        raise HarnessError(f"stages must contain only known stages: {', '.join(STAGES)}")
    if len(set(stages)) != len(stages):
        raise HarnessError("stages must not contain duplicates")
    commands = profile.get("commands", {})
    if not isinstance(commands, dict):
        raise HarnessError("commands must be an object of argument arrays")
    for key, command in commands.items():
        if key not in COMMAND_KEYS:
            raise HarnessError(f"Unsupported command key: {key}")
        _validate_command(key, command)
    maven_args = profile.get("maven_args", [])
    if not isinstance(maven_args, list) or any(not isinstance(item, str) or not item for item in maven_args):
        raise HarnessError("maven_args must be an array of non-empty argument strings")
    if profile.get("sonar_key") is not None and (
        not isinstance(profile["sonar_key"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", profile["sonar_key"])
    ):
        raise HarnessError("sonar_key must contain only safe Sonar project-key characters")
    sonar_languages = profile.get("sonar_languages")
    if sonar_languages is not None and (
        not isinstance(sonar_languages, list) or not sonar_languages
        or any(language not in {"java", "js", "ts"} for language in sonar_languages)
        or len(set(sonar_languages)) != len(sonar_languages)
    ):
        raise HarnessError("sonar_languages must contain unique values from java, js and ts")
    notes = profile.get("notes", [])
    if not isinstance(notes, list) or any(not isinstance(item, str) or not item for item in notes):
        raise HarnessError("notes must be an array of non-empty strings")
    evidence = profile.get("evidence", {})
    if not isinstance(evidence, dict):
        raise HarnessError("evidence must be an object of relative paths")
    for key, value in evidence.items():
        if not isinstance(value, str) or not value:
            raise HarnessError(f"evidence.{key} must be a relative path")
        if safe_relative(value) == Path("."):
            raise HarnessError(f"evidence.{key} must point to a file or generated subdirectory")
    thresholds = profile.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise HarnessError("thresholds must be an object")
    for key in ("lines", "branches"):
        value = thresholds.get(key, 0)
        if not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise HarnessError(f"thresholds.{key} must be between 0 and 100")
    environment = profile.get("environment", {})
    if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
        raise HarnessError("environment values must be strings; use test-only values")
    for key, value in environment.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\x00" in value or "\n" in value or "\r" in value:
            raise HarnessError(f"environment contains an unsafe variable: {key}")
    for forbidden in ("images", "policy", "docker_socket", "host_mounts"):
        if forbidden in profile:
            raise HarnessError(f"Project profile cannot override central security setting: {forbidden}")
    if profile.get("test_reporter") not in (None, "node-junit"):
        raise HarnessError("Only the built-in node-junit test reporter is supported")
    if repo is not None:
        subdir = repo / safe_relative(project["subdir"])
        try:
            inside_repo = subdir.resolve().is_relative_to(repo.resolve())
        except (OSError, ValueError) as exc:
            raise HarnessError(f"Cannot resolve application subdir: {project['subdir']}") from exc
        if not inside_repo or not subdir.is_dir():
            raise HarnessError(f"Application subdir must be an in-repository directory: {project['subdir']}")
    return profile


def load_profile(repo: Path) -> tuple[dict[str, Any], Path]:
    path = profile_path(repo)
    if not path.exists():
        raise HarnessError(f"No project profile at {path}; run ./ci setup --repo {repo}")
    profile = read_json(path)
    if not isinstance(profile, dict):
        raise HarnessError(f"Project profile must be a JSON object: {path}")
    return validate_profile(profile, repo), path


def write_profile(repo: Path, profile: dict[str, Any], overwrite: bool = False) -> Path:
    validate_profile(profile, repo)
    ci_dir = repo / ".ci"
    if ci_dir.is_symlink():
        raise HarnessError("Refusing to write project configuration through a symlink: .ci")
    path = profile_path(repo)
    if path.is_symlink():
        raise HarnessError("Refusing to overwrite a symlinked project profile")
    if path.exists() and not overwrite:
        raise HarnessError(f"Project profile already exists: {path}; use --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, profile)
    return path


def render_agent_prompt(profile: dict[str, Any], repo: Path, harness_cli: Path,
                        report_root: Path) -> str:
    project = profile["project"]
    stages = ", ".join(profile["stages"])
    command = f"{harness_cli} gate --repo {repo} --intent merge-to-main"
    report = report_root / project_slug(profile, repo) / "latest" / "summary.json"
    guidance = [*profile.get("notes", []), *profile_issues(profile)]
    guidance_block = ""
    if guidance:
        guidance_block = "\nVor dem ersten Gate prüfen/erledigen:\n" + "\n".join(
            f"- {item}" for item in guidance
        ) + "\n"
    sonar_block = ""
    if "sonar" in profile["stages"]:
        sonar_block = f"""
Für den ersten Sonar-Lauf muss der lokale Server gestartet und das Profil
provisioniert werden:

    {harness_cli} up
    {harness_cli} bootstrap --repo {repo}

Bei einem frischen lokalen SonarQube rotiert der Bootstrap den Default-Login
automatisch und legt das lokale Admin-Secret nur unter `.local/` mit Modus 0600 ab.
Bei einer bereits angepassten Instanz wird ein vorhandenes Secret verwendet oder
das Admin-Passwort interaktiv abgefragt. Danach darf das Gate den projektspezifischen
Token verwenden. Für die Browseransicht zeigt `{harness_cli} sonar credentials`
den lokalen Read-only-Login an; das Admin-Passwort darf dafür nicht weitergegeben
werden.
"""
    return f"""# Local CI Merge-Gate für {project['name']}

Arbeite im Repository {repo}. Zielbranch ist {profile['target_branch']}.
Verwende den Adapter {project['adapter']} und diese Stages: {stages}.
{guidance_block}
{sonar_block}

Wenn die Aufgabe ausdrücklich den Zustand „bereit zum Mergen nach {profile['target_branch']}"
erreicht, führe aus:

    {command}

Lies anschließend ausschließlich den Report dieses Laufs. Der erwartete Report liegt unter:
{report}

Regeln:

- Prüfe gate.status, source_hash, config_hash, Branch und Run-ID.
- Bei READY ist der aktuelle Arbeitsstand für den nächsten Merge-Schritt geprüft.
- Bei REVIEW, FAIL oder BLOCKED ermittle die Ursache aus den strukturierten Reports und Logs.
- Behebe reproduzierbare Codefehler auf dem aktuellen Branch und führe den Gate-Lauf erneut aus.
- Verändere niemals Tests, Coverage-Schwellen, Scannerregeln, Exclusions oder Timeouts, nur um den Lauf grün zu machen.
- Bei Secrets, Infrastrukturfehlern oder unklaren Security-Befunden stoppen und die Entscheidung melden.
- Nach drei erfolglosen Reparaturversuchen derselben Ursache stoppen.
- Nicht automatisch committen, pushen, mergen oder nach {profile['target_branch']} schreiben.

Ein Lauf auf {profile['target_branch']} vor einem Push verwendet stattdessen:

    {harness_cli} gate --repo {repo} --intent push-main
"""


def write_agent_prompt(repo: Path, profile: dict[str, Any], harness_cli: Path,
                       report_root: Path, overwrite: bool = True) -> Path:
    ci_dir = repo / ".ci"
    if ci_dir.is_symlink():
        raise HarnessError("Refusing to write agent prompt through a symlink: .ci")
    path = prompt_path(repo)
    if path.is_symlink():
        raise HarnessError("Refusing to overwrite a symlinked agent prompt")
    if path.exists() and not overwrite:
        raise HarnessError(f"Agent prompt already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_agent_prompt(profile, repo, harness_cli, report_root), encoding="utf-8")
    return path


def configured_command(profile: dict[str, Any], key: str, default: list[str] | None = None) -> list[str] | None:
    command = profile.get("commands", {}).get(key)
    if command is not None:
        return list(command)
    return list(default) if default is not None else None


def evidence_path(profile: dict[str, Any], key: str, default: str | None = None) -> Path | None:
    value = profile.get("evidence", {}).get(key, default)
    return safe_relative(value) if value else None


def profile_issues(profile: dict[str, Any]) -> list[str]:
    """Return missing evidence prerequisites without silently changing stages."""
    adapter = profile["project"]["adapter"]
    commands = profile.get("commands", {})
    evidence = profile.get("evidence", {})
    issues: list[str] = []
    command_requirements = {
        "lint-typecheck": ("lint", "typecheck"),
        "tests": ("test",),
        "integration": ("integration",),
        "build": ("build",),
        "e2e": ("e2e",),
    }
    for stage, keys in command_requirements.items():
        if stage in profile["stages"]:
            missing = [key for key in keys if key not in commands]
            if missing and adapter in {"custom", "generic"}:
                issues.append(f"{stage}: missing explicit command(s): {', '.join(missing)}")
            elif missing:
                issues.append(f"{stage}: detector found no command(s): {', '.join(missing)}")
    if "tests" in profile["stages"] and "tests" not in evidence:
        issues.append("tests: no test evidence path configured")
    if "integration" in profile["stages"] and "integration" not in evidence:
        issues.append("integration: no integration evidence path configured")
    if "coverage" in profile["stages"]:
        if adapter != "spring-maven" and "coverage" not in commands:
            issues.append("coverage: no coverage command configured")
        if "coverage" not in evidence:
            issues.append("coverage: no coverage evidence path configured")
        if adapter in {"next-npm", "next-fullstack"} and "lcov" not in evidence:
            issues.append("coverage: no LCOV path configured for Sonar")
    if "e2e" in profile["stages"] and adapter not in {"next-npm", "next-fullstack", "custom"}:
        issues.append("e2e: selected adapter has no browser runtime")
    if "e2e" in profile["stages"] and "e2e" not in evidence:
        issues.append("e2e: no browser test evidence path configured")
    return issues
