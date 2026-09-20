#!/usr/bin/env python3
"""Docker-based, local CI entry point. See README.md for prerequisites and scope."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from core import (HarnessError, Runner, Snapshot, capture, frontend_coverage, git,
                  jacoco_coverage, read_json, require_junit, safe_relative, snapshot,
                  tree_hash, validate_semgrep, validate_trivy, verify_unchanged, write_json)
from project import (STAGES, branch_context, configured_command, detect_project, evidence_path,
                     file_sha256, load_profile, profile_from_detection, profile_issues,
                     profile_path, project_slug, prompt_path, repository_root, validate_profile,
                     write_agent_prompt, write_profile)
from sonar import export_analysis, print_ui_credentials, provision, wait_ready
from tui import format_detection, run_setup_tui

ROOT = Path(__file__).resolve().parent.parent
NETWORK = "local-ci-harness"


def load_config() -> dict:
    config = read_json(ROOT / "harness.json")
    if config.get("schema_version") != 1:
        raise HarnessError("Unsupported harness.json schema version")
    if not config.get("projects") or not set(config["projects"]).issubset({"backend", "frontend"}):
        raise HarnessError("Configure at least backend or frontend; supported names are backend/frontend")
    for target, project in config["projects"].items():
        for key in ("path", "subdir", "sonar_key"):
            if not isinstance(project.get(key), str) or not project[key]:
                raise HarnessError(f"Missing {target}.{key}")
        safe_relative(project["subdir"])
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", project["sonar_key"]):
            raise HarnessError("Invalid Sonar project key")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in project.get("environment", {}).items()):
            raise HarnessError("environment values must be strings; use test-only values, never production secrets")
        for k in ("coverage_min_lines", "coverage_min_branches"):
            if not 0 <= project.get(k, -1) <= 100:
                raise HarnessError(f"{target}.{k} must be between 0 and 100")
    runtime_settings(config)
    return config


def source_path(project: dict) -> Path:
    p = Path(project["path"]).expanduser()
    return (ROOT / p).resolve() if not p.is_absolute() else p.resolve()


def local_env() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.exists():
        raise HarnessError("Run ./ci init first")
    result = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def sonar_url() -> str:
    port = int(local_env().get("SONAR_PORT", "9000"))
    if not 1 <= port <= 65535:
        raise HarnessError("Invalid SONAR_PORT")
    return f"http://127.0.0.1:{port}"


def runtime_settings(config: dict | None = None) -> dict[str, Any]:
    """Validate and resolve the trusted host container-runtime settings.

    On macOS Colima is the default runtime. Existing installations without this
    optional field therefore adopt Colima without requiring a config migration;
    Docker Desktop remains an explicit opt-in via ``provider: docker``.
    """
    if config is None:
        config_path = ROOT / "harness.json"
        if config_path.exists():
            config = read_json(config_path)
        else:
            config = {}
    raw = config.get("container_runtime", {})
    if raw is None:
        raw = {}
    if isinstance(raw, str):
        raw = {"provider": raw}
    if not isinstance(raw, dict):
        raise HarnessError("container_runtime must be a provider string or object")
    default_provider = "colima" if platform.system() == "Darwin" else "docker"
    provider = raw.get("provider", default_provider)
    profile = raw.get("profile", "default")
    auto_start = raw.get("auto_start", True)
    if provider not in {"auto", "docker", "colima"}:
        raise HarnessError("container_runtime.provider must be auto, docker or colima")
    if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", profile):
        raise HarnessError("container_runtime.profile must be a safe Colima profile name")
    if not isinstance(auto_start, bool):
        raise HarnessError("container_runtime.auto_start must be boolean")
    return {"provider": provider, "profile": profile, "auto_start": auto_start}


def _colima_state(profile: str) -> str:
    try:
        result = subprocess.run(
            ["colima", "list", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"Cannot query Colima profile {profile}: {exc}") from exc
    if result.returncode:
        return "stopped"
    text = result.stdout.strip()
    if not text:
        return "stopped"
    records: list[Any] = []
    try:
        decoded = json.loads(text)
        records = decoded if isinstance(decoded, list) else [decoded]
    except ValueError:
        # Older Colima versions can emit one JSON object per line for `list`.
        for line in text.splitlines():
            with contextlib.suppress(ValueError):
                records.append(json.loads(line))
    for item in records:
        if isinstance(item, dict) and item.get("name") == profile:
            return str(item.get("status", "stopped")).lower()
    return "stopped"


def _ensure_colima(settings: dict[str, Any]) -> None:
    profile = settings["profile"]
    if not shutil.which("colima"):
        raise HarnessError(
            f"Colima is required for this harness runtime but was not found. "
            f"Install Colima or set container_runtime.provider to docker explicitly"
        )
    state = _colima_state(profile)
    if state != "running":
        if not settings["auto_start"]:
            raise HarnessError(
                f"Colima profile {profile!r} is {state}; start it with "
                f"colima start -p {profile} --runtime docker"
            )
        print(f"Starting Colima profile {profile!r} with Docker runtime...", flush=True)
        try:
            result = subprocess.run(
                ["colima", "start", "-p", profile, "--runtime", "docker"],
                check=False,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HarnessError(f"Cannot start Colima profile {profile}: {exc}") from exc
        if result.returncode:
            raise HarnessError(f"Colima profile {profile} failed to start (exit {result.returncode})")
    socket = Path.home() / ".colima" / profile / "docker.sock"
    os.environ["DOCKER_HOST"] = f"unix://{socket}"
    # DOCKER_HOST is authoritative for all docker/compose subprocesses. Remove
    # a conflicting context selection inherited from the user's shell.
    os.environ.pop("DOCKER_CONTEXT", None)


def ensure_docker(config: dict | None = None) -> None:
    settings = runtime_settings(config)
    provider = settings["provider"]
    if provider == "auto":
        provider = "colima" if platform.system() == "Darwin" else "docker"
    if provider == "colima":
        _ensure_colima(settings)
    if not shutil.which("docker"):
        if provider == "colima":
            raise HarnessError("Docker CLI not found. Install the Docker CLI and Compose v2 for Colima")
        raise HarnessError("Docker CLI not found. Install/start Docker Desktop or Docker Engine + Compose v2")
    try:
        capture(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
    except HarnessError as exc:
        if provider == "colima":
            raise HarnessError(
                f"Docker CLI cannot reach Colima profile {settings['profile']!r} at "
                f"{os.environ.get('DOCKER_HOST')}: {exc}"
            ) from exc
        raise


def ensure_network() -> None:
    p = subprocess.run(["docker", "network", "inspect", NETWORK], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    if p.returncode:
        capture(["docker", "network", "create", NETWORK])


def compose(*args: str) -> None:
    ensure_docker()
    ensure_network()
    lock = read_json(ROOT / "images.lock.json")["images"]
    env = dict(os.environ, **local_env(), POSTGRES_IMAGE=lock["postgres"]["digest"],
               SONARQUBE_IMAGE=lock["sonarqube"]["digest"])
    p = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.yaml"), *args], env=env, check=False)
    if p.returncode:
        raise HarnessError(f"Docker Compose exited {p.returncode}")


def initialize() -> None:
    config = ROOT / "harness.json"
    if not config.exists():
        shutil.copy2(ROOT / "harness.example.json", config)
    env = ROOT / ".env"
    if not env.exists():
        env.write_text(f"POSTGRES_PASSWORD={secrets.token_hex(24)}\nSONAR_PORT=9000\n")
        env.chmod(0o600)
    (ROOT / ".local").mkdir(exist_ok=True)
    (ROOT / ".local").chmod(0o700)
    print("Created configuration without overwriting existing files. Edit harness.json to point to your Git repositories.")
    print("On macOS the harness uses Colima's Docker runtime by default and starts the default profile when needed.")
    print("Then: ./ci doctor; ./ci lock-images; ./ci up; ./ci bootstrap (fresh Sonar admin is rotated automatically); ./ci sonar credentials for the read-only UI login")


def playwright_version(config: dict) -> str:
    project = config["projects"].get("frontend")
    if not project:
        raise HarnessError("No frontend configured")
    lock = read_json(source_path(project) / safe_relative(project["subdir"]) / "package-lock.json")
    version = lock.get("packages", {}).get("node_modules/@playwright/test", {}).get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise HarnessError("Install and lock @playwright/test before locking the matching runtime image (npm lockfile v2/v3 required)")
    return version


def lock_images(config: dict, runtime: bool, update: bool) -> None:
    ensure_docker(config)
    path = ROOT / "images.lock.json"
    prior = read_json(path) if path.exists() else {"images": {}}
    images = dict(config["images"])
    images.pop("zap", None)
    if runtime:
        if "frontend" in config["projects"]:
            images["playwright"] = f"mcr.microsoft.com/playwright:v{playwright_version(config)}-noble"
        images["zap"] = config["images"]["zap"]
    result = dict(prior.get("images", {}))
    for name, reference in images.items():
        if name in result and not update:
            if result[name]["source"] != reference:
                raise HarnessError(f"{name} source changed. Review the update, then run ./ci lock-images --update")
            print(f"Keeping pinned image: {name}")
            continue
        print(f"Resolving {name}: {reference}", flush=True)
        p = subprocess.run(["docker", "pull", reference], check=False)
        if p.returncode:
            raise HarnessError(f"Cannot pull {reference}. Check the tag and CPU architecture")
        info = json.loads(capture(["docker", "image", "inspect", reference]))[0]
        digests = info.get("RepoDigests", [])
        if not digests:
            raise HarnessError(f"No repository digest for {reference}")
        result[name] = {"source": reference, "digest": digests[0],
                        "architecture": info.get("Architecture"), "os": info.get("Os")}
    write_json(path, {"schema_version": 1, "locked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "images": result})
    print("Wrote images.lock.json. Review and commit it. Existing pins only change with --update.")


def policy_hash() -> str:
    paths = [ROOT / "harness.json", ROOT / "images.lock.json", ROOT / "compose.yaml"]
    paths += list((ROOT / "policy").rglob("*")) + list((ROOT / "harness").glob("*.py"))
    files = [p.relative_to(ROOT) for p in paths if p.is_file()]
    return tree_hash(ROOT, files)


def load_install_config() -> dict:
    """Load the trusted installation settings without requiring legacy projects."""
    path = ROOT / "harness.json"
    config = read_json(path)
    if config.get("schema_version") != 1:
        raise HarnessError("Unsupported harness installation schema version")
    if not isinstance(config.get("images"), dict):
        raise HarnessError("harness.json contains no trusted image configuration")
    runtime_settings(config)
    return config


def _profile_command(profile: dict, key: str, default: list[str] | None = None) -> list[str] | None:
    command = configured_command(profile, key, default)
    return list(command) if command is not None else None


def _append_npm_script_args(args: list[str], extra: list[str]) -> list[str]:
    """Append arguments to an npm script without turning them into a shell string."""
    result = list(args)
    if "--" not in result:
        result.append("--")
    result.extend(extra)
    return result


def _append_node_options(existing: str | None, options: list[str]) -> str:
    """Add Node CLI options through NODE_OPTIONS, preserving project options.

    npm appends arguments to the end of the configured script. For commands such
    as ``node --test tests/**/*.test.ts`` that is too late for Node to interpret
    test-runner flags. NODE_OPTIONS is parsed before the script's positional test
    paths and therefore works for both npm scripts and direct node commands.
    """
    additions = shlex.join(options)
    current = (existing or "").strip()
    return f"{current} {additions}".strip()


def _evidence_directory(profile: dict, key: str, default: str) -> Path:
    """Return a safe relative evidence directory used for generated reports."""
    return evidence_path(profile, key, default) or safe_relative(default)


def _junit_output(profile: dict, key: str, default: str, filename: str) -> str:
    return (_evidence_directory(profile, key, default) / filename).as_posix()


def profile_maven_args(profile: dict) -> list[str]:
    values = profile.get("maven_args")
    if values is None and profile.get("project", {}).get("adapter") == "spring-maven":
        return ["-Pci"]
    return list(values or [])


def run_profile_command(r: Runner, profile: dict, snap: Snapshot, name: str, key: str,
                        default: list[str] | None = None, image_override: str | None = None,
                        command_override: list[str] | None = None,
                        extra_args: list[str] | None = None,
                        extra_env: dict[str, str] | None = None) -> bool:
    command = list(command_override) if command_override is not None else _profile_command(profile, key, default)
    if not command:
        r.blocked(name, f"No command configured for stage command {key}")
        return False
    program = command[0].lower()
    if key == "test" and profile.get("test_reporter") == "node-junit":
        if program not in {"npm", "node"} or (program == "npm" and (len(command) < 3 or command[1] != "run")):
            r.blocked(name, "node-junit is only supported for npm run scripts or the node executable")
            return False
    command_specs = {
        "npm": ("node", "npm"),
        "node": ("node", "node"),
        "mvn": ("maven", "mvn"),
        "python": ("python", "python"),
    }
    if program not in command_specs:
        r.blocked(name, f"Unsupported command executable {command[0]}; use an installed adapter or explicit supported runtime")
        return False
    image, entrypoint = command_specs[program]
    image = image_override or image
    args = command[1:]
    if extra_args:
        if program == "npm" and len(command) >= 3 and command[1] == "run":
            args = _append_npm_script_args(args, extra_args)
        else:
            args.extend(extra_args)
    if program == "mvn" and "-Dmaven.repo.local=/cache/maven/repository" not in args:
        args = ["-Dmaven.repo.local=/cache/maven/repository", *args]
    env = dict(profile.get("environment", {}))
    if key == "test" and profile.get("test_reporter") == "node-junit":
        junit_output = _junit_output(profile, "tests", "test-results", "TEST-node.xml")
        (snap.app / _evidence_directory(profile, "tests", "test-results")).mkdir(parents=True, exist_ok=True)
        env["NODE_OPTIONS"] = _append_node_options(
            env.get("NODE_OPTIONS"),
            ["--test-reporter=junit", f"--test-reporter-destination={junit_output}"],
        )
    if extra_env:
        env.update(extra_env)
    mounts: list[tuple[Path, str, bool]] = []
    if image in ("node", "playwright"):
        env["npm_config_cache"] = "/cache/npm"
        mounts.append((cache("npm"), "/cache/npm", False))
    elif image == "maven":
        env.update({"MAVEN_CONFIG": "/cache/maven", "SONAR_USER_HOME": "/cache/sonar"})
        mounts.extend([(cache("maven"), "/cache/maven", False), (cache("sonar"), "/cache/sonar", False)])
    return r.docker(name, image, args, snap, entrypoint=entrypoint, network=NETWORK,
                    env=env, mounts=mounts)


def profile_evidence(r: Runner, profile: dict, snap: Snapshot, key: str,
                     default: str | None = None) -> Path | None:
    relative = evidence_path(profile, key, default)
    if relative is None:
        r.blocked(f"project-{key}-evidence", f"No evidence path configured for {key}")
        return None
    path = snap.app / relative
    if not path.exists():
        r.blocked(f"project-{key}-evidence", f"Missing evidence path: {relative}")
        return None
    return path


def copy_profile_evidence(r: Runner, profile: dict, snap: Snapshot) -> dict:
    names = set(profile.get("evidence", {}).values())
    names.update(("coverage", "test-results", "playwright-report", "target/surefire-reports",
                  "target/failsafe-reports", "target/site/jacoco", "target/bom.json"))
    copied = []
    destination_root = r.reports / "project"
    for name in sorted(names):
        relative = safe_relative(name)
        path = snap.app / relative
        if not path.exists():
            continue
        candidates = [path, *path.rglob("*")] if path.is_dir() else [path]
        if any(p.is_symlink() for p in candidates):
            raise HarnessError(f"Refusing symlink in generated evidence: {name}")
        if not path.resolve().is_relative_to(snap.root.resolve()):
            raise HarnessError(f"Generated evidence escapes snapshot: {name}")
        dest = destination_root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(path, dest)
        copied.append(name)
    return {"copied": copied}


def playwright_version_for_snapshot(snap: Snapshot) -> str:
    lock = read_json(snap.app / "package-lock.json")
    version = lock.get("packages", {}).get("node_modules/@playwright/test", {}).get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise HarnessError("Install and lock @playwright/test before running the browser stage")
    return version


def run_profile_e2e(r: Runner, profile: dict, snap: Snapshot) -> None:
    try:
        version = playwright_version_for_snapshot(snap)
        source = r.lock.get("images", {}).get("playwright", {}).get("source")
        expected = f"mcr.microsoft.com/playwright:v{version}-noble"
        if source != expected:
            r.blocked("project-e2e-runtime", f"Playwright image/package mismatch: expected {expected}; run ./ci lock-images --runtime --update")
            return
        if not run_profile_command(r, profile, snap, "project-e2e-install", "install",
                                   ["npm", "ci", "--no-audit", "--no-fund"], image_override="playwright"):
            r.blocked("project-e2e", "Browser dependency installation failed")
            return
        if not run_profile_command(r, profile, snap, "project-e2e-build", "build",
                                   ["npm", "run", "build"], image_override="playwright"):
            r.blocked("project-e2e", "Browser production build failed")
            return
        e2e_directory = _evidence_directory(profile, "e2e", "test-results")
        e2e_junit = (e2e_directory / "TEST-playwright.xml").as_posix()
        # Keep browser artifacts in a child directory. This prevents Playwright's
        # output cleanup from deleting a unit-test JUnit report when both stages
        # intentionally use the common default test-results directory.
        e2e_artifacts = (e2e_directory / "e2e-artifacts").as_posix()
        run_profile_command(
            r,
            profile,
            snap,
            "project-e2e-tests",
            "e2e",
            ["npm", "run", "test:e2e"],
            image_override="playwright",
            extra_args=["--reporter=list,junit,html", "--output", e2e_artifacts],
            extra_env={
                "PLAYWRIGHT_JUNIT_OUTPUT_FILE": e2e_junit,
                "PLAYWRIGHT_HTML_OPEN": "never",
            },
        )
        evidence = profile_evidence(r, profile, snap, "e2e", "test-results")
        if evidence is not None:
            r.check("project-e2e-evidence", lambda evidence=evidence: require_junit(evidence))
    except (HarnessError, OSError, ValueError, KeyError) as exc:
        r.blocked("project-e2e", str(exc))


def run_profile_sonar(r: Runner, profile: dict, snap: Snapshot, slug: str) -> None:
    reader = ROOT / ".local" / "sonar-reader.token"
    manifest = ROOT / ".local" / "sonar-policy.json"
    if not reader.exists() or not manifest.exists():
        r.blocked("project-sonar", "Run ./ci up and ./ci bootstrap first; scoped Sonar tokens or policy manifest are missing")
        return
    sonar_key = profile.get("sonar_key") or slug
    try:
        policy = read_json(manifest)
        profile_entry = policy.get("profile_projects", {}).get(slug)
    except (HarnessError, AttributeError):
        r.blocked("project-sonar", "Sonar policy manifest is malformed; run bootstrap --repo again")
        return
    if profile_entry is not None and not isinstance(profile_entry, dict):
        r.blocked("project-sonar", "Sonar policy manifest has a malformed project entry; run bootstrap --repo again")
        return
    if profile_entry is not None:
        if profile_entry.get("project_key") != sonar_key:
            r.blocked("project-sonar", "Project profile changed since Sonar bootstrap; run ./ci bootstrap --repo again")
            return
        try:
            token_relative = safe_relative(profile_entry["token_file"])
        except (KeyError, HarnessError):
            r.blocked("project-sonar", "Sonar policy manifest has no safe project token path")
            return
        token_path = (ROOT / token_relative).resolve()
        if token_path.parent != (ROOT / ".local").resolve() or not token_path.is_file():
            r.blocked("project-sonar", "Project-specific Sonar token is missing; run ./ci bootstrap --repo again")
            return
    else:
        token_candidates = [ROOT / ".local" / f"sonar-{slug}.token",
                            ROOT / ".local" / "sonar-frontend.token",
                            ROOT / ".local" / "sonar-backend.token"]
        token_path = next((path for path in token_candidates if path.exists()), None)
        if token_path is None:
            r.blocked("project-sonar", "Project-specific Sonar token is missing; run ./ci bootstrap --repo again")
            return
    output = r.reports / "project"
    output.mkdir(exist_ok=True)
    adapter = profile["project"]["adapter"]
    required_evidence = "coverage" if adapter == "spring-maven" else "lcov" if adapter in {"next-npm", "next-fullstack"} else None
    if required_evidence and profile_evidence(
        r, profile, snap, required_evidence,
        "target/site/jacoco/jacoco.xml" if required_evidence == "coverage" else "coverage/lcov.info",
    ) is None:
        return
    env = {"SONAR_TOKEN": token_path.read_text().strip(), "SONAR_HOST_URL": "http://sonarqube:9000",
           "SONAR_USER_HOME": "/cache/sonar"}
    taskfile = "/reports/project/report-task.txt"
    props = [f"-Dsonar.projectKey={sonar_key}", "-Dsonar.host.url=http://sonarqube:9000",
             "-Dsonar.qualitygate.wait=true", "-Dsonar.qualitygate.timeout=600",
             f"-Dsonar.scanner.metadataFilePath={taskfile}", "-Dsonar.sourceEncoding=UTF-8"]
    if adapter == "spring-maven":
        plugin = r.config.get("plugins", {}).get("sonar_maven")
        if not isinstance(plugin, str) or not plugin:
            r.blocked("project-sonar", "Central sonar_maven plugin version is missing")
            return
        props.append("-Dsonar.coverage.jacoco.xmlReportPaths=target/site/jacoco/jacoco.xml")
        goal = f"org.sonarsource.scanner.maven:sonar-maven-plugin:{plugin}:sonar"
        env.update({"MAVEN_CONFIG": "/cache/maven", "SONAR_USER_HOME": "/cache/sonar"})
        r.docker("project-sonar", "maven", ["-Dmaven.repo.local=/cache/maven/repository", "-B", "-ntp",
                                               *profile_maven_args(profile), *props, goal], snap, env=env,
                 network=NETWORK, entrypoint="mvn",
                 mounts=[(cache("maven"), "/cache/maven", False), (cache("sonar"), "/cache/sonar", False)])
    else:
        source_dir = profile.get("sonar_sources", "src")
        test_inclusions = profile.get("sonar_test_inclusions", "**/*.test.ts,**/*.test.tsx,**/*.spec.ts,**/*.spec.tsx")
        props += [f"-Dsonar.sources={source_dir}", f"-Dsonar.tests={source_dir}",
                  f"-Dsonar.test.inclusions={test_inclusions}", f"-Dsonar.exclusions={test_inclusions}",
                  "-Dsonar.javascript.lcov.reportPaths=coverage/lcov.info"]
        r.docker("project-sonar", "sonar_scanner", props, snap, env=env, network=NETWORK,
                 entrypoint="sonar-scanner", mounts=[(cache("sonar"), "/cache/sonar", False)])
    r.check("project-sonar-evidence",
            lambda: export_analysis(ROOT, output, output / "report-task.txt", sonar_key, sonar_url()))


def run_profile_spring_dependencies(r: Runner, profile: dict, snap: Snapshot, target: str) -> None:
    """Resolve Maven dependencies into a CycloneDX BOM before Trivy scans it."""
    plugin = r.config.get("plugins", {}).get("cyclonedx_maven")
    if not isinstance(plugin, str) or not plugin:
        r.blocked("project-sbom", "Central cyclonedx_maven plugin version is missing")
        return
    goal = f"org.cyclonedx:cyclonedx-maven-plugin:{plugin}:makeAggregateBom"
    bom_ok = run_profile_command(
        r,
        profile,
        snap,
        "project-sbom",
        "integration",
        command_override=["mvn", "-B", "-ntp", *profile_maven_args(profile), goal,
                          "-DoutputFormat=json", "-DincludeTestScope=true"],
    )
    bom = profile_evidence(r, profile, snap, "sbom", "target/bom.json") if bom_ok else None
    if bom is not None:
        trivy(r, target, snap, sbom=True)
    else:
        r.blocked("project-dependencies", "No verified CycloneDX BOM; dependency findings are not trustworthy")
    trivy(r, target, snap, iac=True)


def _gate_status(r: Runner) -> str:
    statuses = [item.status for item in r.results]
    if "BLOCKED" in statuses:
        return "BLOCKED"
    if any(status in ("FAIL", "ERROR") for status in statuses):
        return "FAIL"
    if "WARN" in statuses:
        return "REVIEW"
    return "READY"


def _gate_exit_code(r: Runner, status: str, runner_exit: int) -> int:
    if any(item.status == "ERROR" for item in r.results):
        return 2
    return 0 if status == "READY" else max(1, runner_exit)


def sonar_prerequisites(r: Runner) -> dict[str, str]:
    blocking = [item.name for item in r.results if item.status in ("FAIL", "ERROR", "BLOCKED")]
    if blocking:
        raise HarnessError("Earlier gate stages are incomplete; Sonar analysis is not uploaded: " + ", ".join(blocking))
    return {"prerequisites": "complete"}


def run_profile_pipeline(config: dict, profile: dict, repo: Path, intent: str,
                         start_sonar: Callable[[], Any] | None = None) -> int:
    slug = project_slug(profile, repo)
    context = branch_context(repo, profile["target_branch"], intent)
    profile_file = profile_path(repo)
    metadata = {
        "profile": profile,
        "profile_path": str(profile_file),
        "profile_sha256": file_sha256(profile_file),
        "adapter": profile["project"]["adapter"],
        "stages": profile["stages"],
        "intent": intent,
        "branch": context,
        "policy_sha256": policy_hash(),
    }
    r = Runner(ROOT, config, "gate", slug, namespace=slug, metadata=metadata)
    snap = snapshot(repo, r.work / "project", profile["project"]["subdir"], history=True)
    r.meta["snapshots"]["project"] = {
        "source": str(snap.source),
        "commit": snap.commit,
        "working_tree_sha256": snap.source_hash,
        "includes_uncommitted_changes": True,
        "subdir": profile["project"]["subdir"],
    }
    r.meta["source_hash"] = snap.source_hash
    r.flush()
    adapter = profile["project"]["adapter"]
    stages = profile["stages"]
    runtime_stages = {"lint-typecheck", "tests", "coverage", "build", "e2e", "sonar"}
    if runtime_stages.intersection(stages) and (
        adapter in {"next-npm", "next-fullstack"} or
        (adapter in {"generic", "custom"} and "install" in profile.get("commands", {}))
    ):
        installed = run_profile_command(r, profile, snap, "project-install", "install",
                                        ["npm", "ci", "--no-audit", "--no-fund"])
        if not installed:
            r.blocked("project-runtime", "Project dependency installation failed")
    if "secrets" in stages:
        secret_scans(r, slug, snap, full=True)
    if "static-analysis" in stages:
        sast(r, slug, snap)
    if "dependencies" in stages and adapter != "spring-maven":
        trivy(r, slug, snap)
        trivy(r, slug, snap, iac=True)
    if "lint-typecheck" in stages:
        run_profile_command(r, profile, snap, "project-lint", "lint")
        run_profile_command(r, profile, snap, "project-typecheck", "typecheck")
    if "tests" in stages:
        test_ok = run_profile_command(r, profile, snap, "project-tests", "test")
        evidence = profile_evidence(r, profile, snap, "tests", "test-results")
        if test_ok and evidence is not None:
            r.check("project-tests-evidence", lambda evidence=evidence: require_junit(evidence))
    if "integration" in stages:
        integration_ok = run_profile_command(r, profile, snap, "project-integration", "integration")
        evidence = profile_evidence(r, profile, snap, "integration")
        if integration_ok and evidence is not None:
            r.check("project-integration-evidence", lambda evidence=evidence: require_junit(evidence))
    build_ok = True
    if "build" in stages:
        build_ok = run_profile_command(r, profile, snap, "project-build", "build")
    maven_verified = adapter == "spring-maven" and "build" in stages and build_ok
    if "coverage" in stages:
        coverage_command_ok = True
        if adapter == "spring-maven":
            if not maven_verified:
                coverage_command_ok = run_profile_command(
                    r, profile, snap, "project-coverage-command", "coverage",
                    ["mvn", "-B", "-ntp", *profile_maven_args(profile), "verify"],
                )
            maven_verified = maven_verified or coverage_command_ok
        else:
            coverage_command_ok = run_profile_command(r, profile, snap, "project-coverage-command", "coverage")
        coverage_file = profile_evidence(
            r,
            profile,
            snap,
            "coverage",
            "target/site/jacoco/jacoco.xml" if adapter == "spring-maven" else "coverage/coverage-summary.json",
        ) if coverage_command_ok else None
        if coverage_command_ok and coverage_file is not None:
            if adapter == "spring-maven":
                r.check("project-coverage", lambda coverage_file=coverage_file: jacoco_coverage(
                    coverage_file, profile["thresholds"]["lines"], profile["thresholds"]["branches"]))
            else:
                r.check("project-coverage", lambda coverage_file=coverage_file: frontend_coverage(
                    coverage_file, profile["thresholds"]["lines"], profile["thresholds"]["branches"]))
        if "sonar" in stages and adapter in {"next-npm", "next-fullstack"}:
            lcov = profile_evidence(r, profile, snap, "lcov", "coverage/lcov.info")
            if lcov is None:
                r.blocked("project-lcov", "LCOV report missing; Sonar coverage import is not trustworthy")
    if adapter == "spring-maven" and "tests" in stages:
        if not maven_verified:
            r.blocked("project-integration-evidence", "Spring integration evidence requires a successful Maven verify/build stage")
        else:
            integration = profile_evidence(r, profile, snap, "integration", "target/failsafe-reports")
            if integration is not None:
                r.check("project-integration-evidence", lambda integration=integration: require_junit(integration))
    if adapter == "spring-maven" and "dependencies" in stages:
        if maven_verified:
            run_profile_spring_dependencies(r, profile, snap, slug)
        else:
            r.blocked("project-dependencies", "Maven verify did not complete; no trustworthy resolved dependency inventory")
            trivy(r, slug, snap, iac=True)
    if "e2e" in stages:
        run_profile_e2e(r, profile, snap)
    if "zap" in stages:
        r.blocked("project-zap", "Use ./ci zap explicitly with an acknowledged local test target")
    if "sonar" in stages:
        if r.check("project-sonar-prerequisites", lambda: sonar_prerequisites(r)):
            sonar_ready = True
            if start_sonar is not None:
                sonar_ready = r.check("project-sonar-service", start_sonar)
            if sonar_ready:
                run_profile_sonar(r, profile, snap, slug)
    r.check("project-artifacts", lambda: copy_profile_evidence(r, profile, snap))
    r.check("project-source-unchanged", lambda: verify_unchanged(snap))
    r.check("policy-unchanged", lambda: _require_equal(metadata["policy_sha256"], policy_hash(),
                                                       "Harness policy changed during the run"))
    r.meta["gate"] = {
        "status": _gate_status(r),
        "target_branch": profile["target_branch"],
        "source_hash": snap.source_hash,
        "config_hash": metadata["profile_sha256"],
        "policy_hash": metadata["policy_sha256"],
    }
    exit_code = r.finish()
    return _gate_exit_code(r, r.meta["gate"]["status"], exit_code)


def prepare(runner: Runner, target: str, full: bool) -> Snapshot:
    project = runner.config["projects"][target]
    snap = snapshot(source_path(project), runner.work / target, project["subdir"], history=full)
    runner.meta["snapshots"][target] = {"source": str(snap.source), "commit": snap.commit,
                                        "working_tree_sha256": snap.source_hash,
                                        "includes_uncommitted_changes": True, "subdir": project["subdir"]}
    runner.flush()
    return snap


def cache(name: str) -> Path:
    path = ROOT / ".local" / "cache" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def maven(r: Runner, name: str, snap: Snapshot, goals: list[str], extra_env: dict | None = None,
          with_project_args: bool = True) -> bool:
    project = r.config["projects"]["backend"]
    env = dict(project.get("environment", {}), MAVEN_CONFIG="/cache/maven", SONAR_USER_HOME="/cache/sonar")
    if extra_env:
        env.update(extra_env)
    args = ["-B", "-ntp", "-Dmaven.repo.local=/cache/maven/repository"]
    if with_project_args:
        args += project.get("maven_args", [])
    return r.docker(name, "maven", args + goals, snap, entrypoint="mvn", network=NETWORK, env=env,
                    mounts=[(cache("maven"), "/cache/maven", False), (cache("sonar"), "/cache/sonar", False)])


def node(r: Runner, name: str, snap: Snapshot, args: list[str], image: str = "node") -> bool:
    env = dict(r.config["projects"]["frontend"].get("environment", {}), npm_config_cache="/cache/npm")
    return r.docker(name, image, args, snap, entrypoint="npm", network=NETWORK, env=env,
                    mounts=[(cache("npm"), "/cache/npm", False)])


def check_backend_contract(snap: Snapshot) -> dict:
    import xml.etree.ElementTree as ET
    path = snap.app / "pom.xml"
    if not path.exists():
        raise HarnessError("No backend pom.xml")
    root = ET.parse(path).getroot()
    if root.find("{*}modules") is not None:
        raise HarnessError("This first adapter supports a single Maven module. Multi-module needs aggregate test/coverage paths and a same-reactor Sonar invocation; see technical_doc.md")
    return {"pom": "pom.xml", "adapter": "single-module Maven"}


def check_frontend_contract(snap: Snapshot, full: bool) -> dict:
    package = read_json(snap.app / "package.json")
    lock = read_json(snap.app / "package-lock.json")
    if lock.get("lockfileVersion", 0) < 2:
        raise HarnessError("npm lockfile v2/v3 is required; pnpm/yarn need a dedicated adapter")
    scripts = package.get("scripts", {})
    expected = {"lint", "typecheck", "test:ci" if full else "test:unit"}
    if full:
        expected |= {"build", "format:check"}
    missing = expected - scripts.keys()
    if missing:
        raise HarnessError(f"Missing frontend scripts: {sorted(missing)}; see templates/frontend/package-scripts.json")
    dependencies = package.get("dependencies", {})
    if "next" not in dependencies and "next" not in package.get("devDependencies", {}):
        raise HarnessError("Expected a Next.js project")
    return {"required_scripts": sorted(expected), "package_manager": "npm"}


def redact_and_check_gitleaks(path: Path) -> dict:
    findings = read_json(path)
    if not isinstance(findings, list):
        raise HarnessError("Invalid Gitleaks report")
    for item in findings:
        for key in ("Secret", "Match", "secret", "match"):
            if key in item:
                item[key] = "[REDACTED]"
    write_json(path, findings)
    if findings:
        raise HarnessError(f"Gitleaks found {len(findings)} potential secrets. Values redacted; rotate real exposed credentials")
    return {"findings": 0}


def secret_scans(r: Runner, target: str, snap: Snapshot, full: bool) -> None:
    modes = ["dir", "git"] if full else ["dir"]
    for mode in modes:
        name = f"{target}-secrets-{mode}"
        if mode == "git" and git(snap.root, "rev-parse", "--is-shallow-repository") == "true":
            r.blocked(name, "Shallow Git history. Fetch the required history explicitly; an incomplete history is not a full secret scan")
            continue
        args = [mode, "/workspace", "--config", "/policy/gitleaks.toml", "--gitleaks-ignore-path",
                "/policy/gitleaks.ignore", "--ignore-gitleaks-allow", "--redact=100", "--no-banner",
                "--report-format", "json", "--report-path", f"/reports/{name}.json", "--exit-code", "1"]
        if mode == "git":
            args += ["--log-opts=--all"]
        r.docker(name, "gitleaks", args, snap, readonly_source=True, network="none")
        r.check(name + "-evidence", lambda name=name: redact_and_check_gitleaks(r.reports / f"{name}.json"))


def sast(r: Runner, target: str, snap: Snapshot) -> None:
    name = f"{target}-semgrep"
    rel = snap.app.relative_to(snap.root).as_posix()
    local_ignores = [str(p.relative_to(snap.root)) for p in snap.root.rglob(".semgrepignore") if ".git" not in p.parts]
    if local_ignores:
        r.blocked(name, f"Project-local .semgrepignore could weaken the central policy: {local_ignores}. Review and move approved exclusions into the harness policy")
        return
    extra_configs = []
    for file in sorted((ROOT / "policy" / "semgrep-extra").glob("*.y*ml")):
        extra_configs += ["--config", "/policy/semgrep-extra/" + file.name]
    args = ["scan", "--config", "/policy/semgrep.yml", *extra_configs, "--metrics", "off", "--strict", "--disable-nosem",
            "--disable-version-check", "--oss-only", "--max-target-bytes", "0",
            "--no-git-ignore", "--exclude", ".git", "--exclude", "node_modules", "--exclude", "target",
            "--exclude", ".next", "--json", "--output", f"/reports/{name}.json", f"/workspace/{rel}"]
    r.docker(name, "semgrep", args, snap, readonly_source=True, network="none", entrypoint="semgrep")
    r.check(name + "-evidence", lambda: validate_semgrep(r.reports / f"{name}.json"))


def trivy(r: Runner, target: str, snap: Snapshot, sbom: bool = False, iac: bool = False) -> None:
    name = f"{target}-" + ("iac" if iac else "dependencies")
    args = ["sbom" if sbom else "fs", "--cache-dir", "/cache/trivy", "--format", "json", "--exit-code", "1",
            "--severity", "HIGH,CRITICAL", "--ignorefile", "/policy/trivy.ignore", "--output", f"/reports/{name}.json"]
    if not sbom:
        args += ["--scanners", "misconfig" if iac else "vuln", "--skip-dirs", "node_modules", "--skip-dirs", ".git",
                 "--skip-dirs", ".next", "--skip-dirs", "target"]
        if not iac:
            args += ["--include-dev-deps"]
    if not iac:
        args += ["--list-all-pkgs"]
    app = "/workspace/" + snap.app.relative_to(snap.root).as_posix()
    args += [app + "/target/bom.json" if sbom else app]
    r.docker(name, "trivy", args, snap, readonly_source=True,
             mounts=[(cache("trivy"), "/cache/trivy", False)])
    r.check(name + "-evidence", lambda: validate_trivy(r.reports / f"{name}.json", need_packages=not iac))


def copy_evidence(r: Runner, target: str, snap: Snapshot) -> dict:
    sources = (["target/surefire-reports", "target/failsafe-reports", "target/site/jacoco", "target/bom.json"]
               if target == "backend" else ["coverage", "test-results", "playwright-report"])
    copied = []
    for name in sources:
        path = snap.app / name
        if path.exists():
            # Never follow links created by a build back into the host filesystem.
            candidates = [path, *path.rglob("*")] if path.is_dir() else [path]
            if any(p.is_symlink() for p in candidates):
                raise HarnessError(f"Refusing symlink in generated evidence: {name}")
            if not path.resolve().is_relative_to(snap.root.resolve()):
                raise HarnessError(f"Generated evidence escapes snapshot: {name}")
            dest = r.reports / target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(path, dest)
            copied.append(name)
    return {"copied": copied}


def sonar_analysis(r: Runner, target: str, snap: Snapshot) -> None:
    project = r.config["projects"][target]
    token_path = ROOT / ".local" / f"sonar-{target}.token"
    if not token_path.exists() or not (ROOT / ".local" / "sonar-reader.token").exists() or not (ROOT / ".local" / "sonar-policy.json").exists():
        r.blocked(f"{target}-sonar", "Run ./ci up and ./ci bootstrap first; missing scoped tokens or successful policy manifest")
        return
    output = r.reports / target
    output.mkdir(exist_ok=True)
    env = {"SONAR_TOKEN": token_path.read_text().strip(), "SONAR_HOST_URL": "http://sonarqube:9000",
           "SONAR_USER_HOME": "/cache/sonar"}
    taskfile = f"/reports/{target}/report-task.txt"
    props = [f"-Dsonar.projectKey={project['sonar_key']}", "-Dsonar.host.url=http://sonarqube:9000",
             "-Dsonar.qualitygate.wait=true", "-Dsonar.qualitygate.timeout=600",
             f"-Dsonar.scanner.metadataFilePath={taskfile}", "-Dsonar.sourceEncoding=UTF-8"]
    if target == "backend":
        goal = f"org.sonarsource.scanner.maven:sonar-maven-plugin:{r.config['plugins']['sonar_maven']}:sonar"
        props += [f"-Dsonar.coverage.jacoco.xmlReportPaths={project['coverage_xml']}"]
        maven(r, "backend-sonar", snap, [goal, *props], env)
    else:
        tests = project.get("sonar_test_inclusions", "**/*.test.ts,**/*.test.tsx")
        props += [f"-Dsonar.sources={project['sonar_sources']}", f"-Dsonar.tests={project['sonar_sources']}",
                  f"-Dsonar.test.inclusions={tests}", f"-Dsonar.exclusions={tests}",
                  "-Dsonar.javascript.lcov.reportPaths=coverage/lcov.info"]
        r.docker("frontend-sonar", "sonar_scanner", props, snap, env=env, network=NETWORK,
                 entrypoint="sonar-scanner", mounts=[(cache("sonar"), "/cache/sonar", False)])
    r.check(f"{target}-sonar-evidence", lambda: export_analysis(ROOT, output, output / "report-task.txt",
                                                              project["sonar_key"], sonar_url()))


def backend_pipeline(r: Runner, snap: Snapshot, full: bool) -> None:
    project = r.config["projects"]["backend"]
    if not r.check("backend-contract", lambda: check_backend_contract(snap)):
        r.blocked("backend-build", "Backend project contract failed")
        return
    secret_scans(r, "backend", snap, full)
    if full:
        sast(r, "backend", snap)
    built = maven(r, "backend-verify" if full else "backend-unit", snap,
                  ["clean", "verify"] if full else ["test"])
    tests_ok = r.check("backend-unit-evidence", lambda: require_junit(snap.app / "target/surefire-reports"))
    if full:
        integration_ok = True
        if project.get("require_integration_tests", True):
            integration_ok = r.check("backend-integration-evidence", lambda: require_junit(snap.app / "target/failsafe-reports"))
        coverage_ok = r.check("backend-coverage", lambda: jacoco_coverage(snap.app / safe_relative(project["coverage_xml"]),
                                                   project["coverage_min_lines"], project["coverage_min_branches"]))
        if built:
            goal = f"org.cyclonedx:cyclonedx-maven-plugin:{r.config['plugins']['cyclonedx_maven']}:makeAggregateBom"
            bom = maven(r, "backend-sbom", snap, [goal, "-DoutputFormat=json", "-DincludeTestScope=true"])
            if bom:
                trivy(r, "backend", snap, sbom=True)
            else:
                r.blocked("backend-dependencies", "SBOM generation failed")
        else:
            r.blocked("backend-dependencies", "Build failed; no verified resolved dependency inventory")
        trivy(r, "backend", snap, iac=True)
        if built and tests_ok and integration_ok and coverage_ok:
            sonar_analysis(r, "backend", snap)
        else:
            r.blocked("backend-sonar", "Build/test/coverage evidence incomplete; not uploading a misleading analysis")
    r.check("backend-artifacts", lambda: copy_evidence(r, "backend", snap))


def frontend_pipeline(r: Runner, snap: Snapshot, full: bool) -> None:
    project = r.config["projects"]["frontend"]
    if not r.check("frontend-contract", lambda: check_frontend_contract(snap, full)):
        r.blocked("frontend-install", "Frontend project contract failed")
        return
    secret_scans(r, "frontend", snap, full)
    if full:
        sast(r, "frontend", snap)
    if not node(r, "frontend-install", snap, ["ci", "--no-audit", "--no-fund"]):
        r.blocked("frontend-tests-and-build", "npm ci failed; no fallback to npm install")
        return
    node(r, "frontend-lint", snap, ["run", "lint"])
    node(r, "frontend-typecheck", snap, ["run", "typecheck"])
    tested = node(r, "frontend-unit", snap, ["run", "test:ci" if full else "test:unit"])
    tests_ok = r.check("frontend-unit-evidence", lambda: require_junit(snap.app / "test-results"))
    if full:
        node(r, "frontend-format", snap, ["run", "format:check"])
        built = node(r, "frontend-build", snap, ["run", "build"])
        coverage_ok = r.check("frontend-coverage", lambda: frontend_coverage(snap.app / "coverage/coverage-summary.json",
                                                            project["coverage_min_lines"], project["coverage_min_branches"]))
        if not (snap.app / "coverage/lcov.info").is_file():
            r.blocked("frontend-lcov", "LCOV report missing; cannot import coverage into Sonar")
            coverage_ok = False
        trivy(r, "frontend", snap)
        trivy(r, "frontend", snap, iac=True)
        if built and tested and tests_ok and coverage_ok:
            sonar_analysis(r, "frontend", snap)
        else:
            r.blocked("frontend-sonar", "Build/test/coverage evidence incomplete")
    r.check("frontend-artifacts", lambda: copy_evidence(r, "frontend", snap))


def e2e_pipeline(r: Runner, snap: Snapshot) -> None:
    version = playwright_version(r.config)
    source = r.lock["images"].get("playwright", {}).get("source")
    if source != f"mcr.microsoft.com/playwright:v{version}-noble":
        raise HarnessError("Playwright image/package version mismatch. Review lockfile, then ./ci lock-images --runtime --update")
    if not node(r, "e2e-install", snap, ["ci", "--no-audit", "--no-fund"], image="playwright"):
        r.blocked("e2e-browser-tests", "Browser test installation failed")
        return
    # Rebuild inside the browser image: never reuse native node_modules across Node ABIs.
    if not node(r, "e2e-production-build", snap, ["run", "build"], image="playwright"):
        r.blocked("e2e-browser-tests", "Production build failed")
        return
    script = r.config["projects"]["frontend"].get("e2e_script", "test:e2e")
    node(r, "e2e-browser-tests", snap, ["run", script], image="playwright")
    r.check("e2e-test-evidence", lambda: require_junit(snap.app / "test-results"))
    r.check("e2e-artifacts", lambda: copy_evidence(r, "frontend", snap))


def pipeline(config: dict, mode: str, target: str) -> int:
    ensure_docker(config)
    ensure_network()
    chosen = list(config["projects"]) if target == "all" else [target]
    if any(t not in config["projects"] for t in chosen):
        raise HarnessError(f"Project {target} is not configured")
    if mode == "e2e" and chosen != ["frontend"]:
        raise HarnessError("Browser E2E supports frontend; backend integration tests run in full backend")
    r = Runner(ROOT, config, mode, target)
    initial_policy_hash = policy_hash()
    r.meta["policy_sha256"] = initial_policy_hash
    snapshots = []
    try:
        for item in chosen:
            try:
                snap = prepare(r, item, full=mode == "full")
                snapshots.append((item, snap))
                if mode == "e2e":
                    e2e_pipeline(r, snap)
                elif item == "backend":
                    backend_pipeline(r, snap, mode == "full")
                else:
                    frontend_pipeline(r, snap, mode == "full")
            except (HarnessError, OSError, ValueError, KeyError) as exc:
                r.blocked(item + "-pipeline", str(exc))
        for item, snap in snapshots:
            r.check(item + "-source-unchanged", lambda snap=snap: verify_unchanged(snap))
        r.check("policy-unchanged", lambda: _require_equal(initial_policy_hash, policy_hash(), "Harness policy changed during the run"))
    except KeyboardInterrupt:
        r.blocked("interrupted", "User interrupted the run; no complete result")
    return r.finish()


def _require_equal(a: Any, b: Any, message: str) -> dict:
    if a != b:
        raise HarnessError(message)
    return {"unchanged": True}


def validate_zap_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    allowed = {"frontend", "backend", "host.docker.internal", "127.0.0.1", "localhost"}
    if parsed.scheme not in ("http", "https") or parsed.hostname not in allowed or parsed.username or parsed.password:
        raise HarnessError("ZAP targets must be a local test service: frontend/backend on the harness network or host.docker.internal. No public domains or credentials in URLs")
    if parsed.hostname in ("localhost", "127.0.0.1"):
        raise HarnessError("localhost inside ZAP is the ZAP container, not your app. Use a Docker service alias or host.docker.internal")
    return url


def validate_zap_report(path: Path) -> dict:
    sites = read_json(path).get("site", [])
    if not isinstance(sites, list) or not sites:
        raise HarnessError("ZAP reported no scanned sites; an empty report is not a pass")
    return {"sites": len(sites), "report": path.name}


def zap_scan(config: dict, url: str, acknowledged: bool) -> int:
    if not acknowledged:
        raise HarnessError("Pass --ack-local-test-target for an owned, disposable local test deployment; never use production data")
    validate_zap_url(url)
    ensure_docker(config)
    ensure_network()
    r = Runner(ROOT, config, "zap-baseline", "local-test-deployment")
    # Own container network, no Docker socket. Mount output where packaged ZAP expects it.
    r.docker("zap-baseline", "zap", ["-t", url, "-m", "1", "-T", "5", "-c", "/policy/zap.conf",
             "-J", "zap.json", "-r", "zap.html"], network=NETWORK, entrypoint="zap-baseline.py",
             mounts=[(r.reports, "/zap/wrk", False)], allowed_codes=(0, 2), rootfs_readonly=False,
             host_gateway=urllib.parse.urlsplit(url).hostname == "host.docker.internal")
    r.check("zap-report-evidence", lambda: validate_zap_report(r.reports / "zap.json"))
    return r.finish()


def doctor(config: dict) -> None:
    print("Host Python:", sys.version.split()[0])
    print("Git:", capture(["git", "--version"]))
    settings = runtime_settings(config)
    provider = settings["provider"]
    if provider == "auto":
        provider = "colima" if platform.system() == "Darwin" else "docker"
    print(f"Container runtime: {provider}" + (f" ({settings['profile']})" if provider == "colima" else ""))
    ensure_docker(config)
    print("Docker:", capture(["docker", "version", "--format", "{{.Server.Version}}/{{.Server.Arch}}"], timeout=30))
    print("Compose:", capture(["docker", "compose", "version"]))
    for target, project in config["projects"].items():
        path = source_path(project)
        print(f"{target}: {path} / {project['subdir']}")
        print("  Git root:", git(path, "rev-parse", "--show-toplevel"))
        if not (path / safe_relative(project["subdir"])).is_dir():
            raise HarnessError(f"Missing {target} subdir")
    print("CPU architecture compatibility and Sonar API compatibility are checked again when images start.")


def setup_project(repo_arg: str, force: bool, noninteractive: bool,
                  adapter: str | None, stages_arg: str | None, subdir: str = ".") -> int:
    repo = repository_root(repo_arg)
    detection = detect_project(repo)
    print(format_detection(detection))
    if noninteractive:
        stages = [item.strip() for item in stages_arg.split(",")] if stages_arg else None
        profile = profile_from_detection(detection, adapter=adapter, stages=stages)
        run_now = False
    else:
        profile, run_now = run_setup_tui(detection)
    profile["project"]["subdir"] = subdir
    validate_profile(profile, repo)
    profile_path_written = write_profile(repo, profile, overwrite=force)
    prompt = write_agent_prompt(repo, profile, ROOT / "ci", ROOT / "reports")
    print(f"Wrote project profile: {profile_path_written}")
    print(f"Wrote agent prompt: {prompt}")
    for note in profile.get("notes", []):
        print(f"REVIEW: {note}")
    for issue in profile_issues(profile):
        print(f"REVIEW: {issue}")
    if run_now:
        intent = "push-main" if profile["target_branch"] == project_current_branch(repo) else "merge-to-main"
        print(f"Starting requested first gate with intent {intent}")
        return gate_project(str(repo), intent)
    next_intent = "push-main" if profile["target_branch"] == project_current_branch(repo) else "merge-to-main"
    print(f"Next: ./ci gate --repo {repo} --intent {next_intent}")
    return 0


def project_current_branch(repo: Path) -> str:
    from project import current_branch
    return current_branch(repo)


def prompt_project(repo_arg: str) -> int:
    repo = repository_root(repo_arg)
    profile, _ = load_profile(repo)
    prompt = write_agent_prompt(repo, profile, ROOT / "ci", ROOT / "reports")
    print(prompt.read_text(encoding="utf-8"))
    return 0


def gate_project(repo_arg: str, intent: str) -> int:
    repo = repository_root(repo_arg)
    profile, _ = load_profile(repo)
    config = load_install_config()
    ensure_docker(config)
    ensure_network()
    sonar_lifecycle = "sonar" in profile["stages"]
    sonar_started = False

    def start_sonar() -> dict[str, str]:
        nonlocal sonar_started
        compose("up", "-d")
        sonar_started = True
        wait_ready(sonar_url())
        return {"service": "sonarqube", "url": sonar_url()}

    result = 2
    try:
        # Sonar is started only after all application stages have passed. This
        # keeps the memory-heavy service stack out of npm/build/E2E execution.
        result = run_profile_pipeline(
            config, profile, repo, intent,
            start_sonar=start_sonar if sonar_lifecycle else None,
        )
    finally:
        if sonar_started:
            # Compose down removes the service containers but intentionally keeps
            # sonar-data/sonar-postgres/sonar-extensions volumes for the next run.
            compose("down", "--remove-orphans")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor", "up", "down"):
        sub.add_parser(name)
    bootstrap = sub.add_parser("bootstrap", help="Provision the legacy projects or one project profile in SonarQube")
    bootstrap.add_argument("--repo", help="Git repository containing .ci/harness.json")
    sonar_cmd = sub.add_parser("sonar", help="Manage the local SonarQube instance")
    sonar_sub = sonar_cmd.add_subparsers(dest="sonar_command", required=True)
    sonar_sub.add_parser("credentials", help="Print the least-privilege SonarQube UI credentials")
    setup = sub.add_parser("setup", help="Create a project profile with the interactive setup TUI")
    setup.add_argument("--repo", default=".", help="Git repository path")
    setup.add_argument("--force", action="store_true", help="Replace an existing .ci/harness.json")
    setup.add_argument("--non-interactive", action="store_true", help="Use detection defaults without curses")
    setup.add_argument("--adapter", choices=("generic", "next-npm", "next-fullstack", "spring-maven", "custom"))
    setup.add_argument("--stages", help="Comma-separated stage names for non-interactive setup")
    setup.add_argument("--subdir", default=".", help="Application subdirectory inside the Git repository (for monorepos)")
    prompt = sub.add_parser("prompt", help="Regenerate and print the project agent prompt")
    prompt.add_argument("--repo", default=".", help="Git repository path")
    gate = sub.add_parser("gate", help="Run the selected project profile as a merge gate")
    gate.add_argument("--repo", default=".", help="Git repository path")
    gate.add_argument("--intent", choices=("merge-to-main", "push-main"), required=True)
    lock = sub.add_parser("lock-images")
    lock.add_argument("--runtime", action="store_true", help="Also lock matching Playwright and ZAP images")
    lock.add_argument("--update", action="store_true", help="Explicitly replace existing image digests; review Sonar upgrade compatibility first")
    for name in ("quick", "full", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("target", choices=("backend", "frontend", "all"), default="all", nargs="?")
    zap = sub.add_parser("zap")
    zap.add_argument("url")
    zap.add_argument("--ack-local-test-target", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        raise HarnessError("Python 3.10+ required")
    if args.command == "init":
        initialize()
        return 0
    if args.command in ("setup", "prompt"):
        (ROOT / ".local").mkdir(exist_ok=True)
        with (ROOT / ".local" / "run.lock").open("w") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HarnessError("Another harness command is running; simultaneous local runs are not supported") from exc
            if args.command == "setup":
                return setup_project(args.repo, args.force, args.non_interactive, args.adapter, args.stages, args.subdir)
            return prompt_project(args.repo)
    if args.command == "gate":
        (ROOT / ".local").mkdir(exist_ok=True)
        with (ROOT / ".local" / "run.lock").open("w") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HarnessError("Another harness command is running; simultaneous local runs are not supported") from exc
            return gate_project(args.repo, args.intent)
    config = load_install_config() if args.command == "sonar" or (args.command == "bootstrap" and args.repo) else load_config()
    # Prevent simultaneous jobs corrupting caches, Sonar's single main-branch result,
    # or the reports/latest pointer. fcntl is available on macOS/Linux/WSL2.
    (ROOT / ".local").mkdir(exist_ok=True)
    with (ROOT / ".local" / "run.lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HarnessError("Another harness command is running; simultaneous local runs are not supported") from exc
        if args.command == "doctor":
            doctor(config)
        elif args.command == "lock-images":
            lock_images(config, args.runtime, args.update)
        elif args.command == "up":
            compose("up", "-d")
            wait_ready(sonar_url())
            print("SonarQube:", sonar_url())
        elif args.command == "down":
            compose("down")
            print("Stopped services. Persistent Sonar data retained; no volumes deleted.")
        elif args.command == "bootstrap":
            if args.repo:
                profile_repo = repository_root(args.repo)
                load_profile(profile_repo)
                provision(ROOT, config, sonar_url(), profile_repo=profile_repo)
            else:
                provision(ROOT, config, sonar_url())
        elif args.command == "sonar":
            if args.sonar_command == "credentials":
                print_ui_credentials(ROOT, sonar_url())
        elif args.command in ("quick", "full", "e2e"):
            return pipeline(config, args.command, args.target)
        elif args.command == "zap":
            return zap_scan(config, args.url, args.ack_local_test_target)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HarnessError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Interrupted; no passing result.", file=sys.stderr)
        sys.exit(130)
