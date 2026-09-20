"""Provision a private, local SonarQube instance and export machine-readable evidence.

Uses the server's own Web API parameter metadata for gateName/gateId transitions.
A rejected API call fails provisioning instead of leaving a silently partial policy.
"""
from __future__ import annotations

import base64
import datetime as dt
import getpass
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from core import HarnessError, read_json, write_json


class Sonar:
    def __init__(self, url: str, token: str | None = None, login: str | None = None,
                 password: str | None = None):
        self.url = url.rstrip("/")
        if token:
            self.auth = "Bearer " + token
        elif login is not None:
            basic = base64.b64encode(f"{login}:{password or ''}".encode()).decode()
            self.auth = "Basic " + basic
        else:
            self.auth = None
        self._parameters: dict[str, set[str]] | None = None

    def call(self, endpoint: str, params: dict[str, Any] | None = None, post: bool = False) -> dict:
        encoded = urllib.parse.urlencode(params or {}).encode()
        url = self.url + "/api/" + endpoint
        data = encoded if post else None
        if encoded and not post:
            url += "?" + encoded.decode()
        headers = {"Accept": "application/json"}
        if self.auth:
            headers["Authorization"] = self.auth
        if post:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if post else "GET")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            # API errors must never contain the auth header/token in reports.
            body = exc.read().decode(errors="replace")[:1500]
            raise HarnessError(f"Sonar API {endpoint}: HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            raise HarnessError(f"Sonar API {endpoint}: {exc}") from exc

    def post(self, endpoint: str, **params: Any) -> dict:
        return self.call(endpoint, params, post=True)

    def parameters(self, endpoint: str) -> set[str]:
        if self._parameters is None:
            data = self.call("webservices/list")
            self._parameters = {}
            for service in data.get("webServices", []):
                path = service["path"].removeprefix("api/")
                for action in service.get("actions", []):
                    self._parameters[path + "/" + action["key"]] = {p["key"] for p in action.get("params", [])}
        return self._parameters.get(endpoint, set())

    def gate_post(self, endpoint: str, gate: dict, **params: Any) -> dict:
        keys = self.parameters(endpoint)
        if "gateName" in keys:
            params["gateName"] = gate["name"]
        elif "gateId" in keys:
            params["gateId"] = gate["id"]
        else:
            raise HarnessError(f"Unsupported Sonar gate API {endpoint}; review this server version before provisioning")
        return self.post(endpoint, **params)


def wait_ready(url: str, seconds: int = 300) -> None:
    client = Sonar(url)
    deadline = time.monotonic() + seconds
    last = "not ready"
    while time.monotonic() < deadline:
        try:
            data = client.call("system/status")
            last = data.get("status", "unknown")
            if last == "UP":
                return
            if last in ("DB_MIGRATION_NEEDED", "DB_MIGRATION_RUNNING"):
                raise HarnessError("SonarQube database migration needs operator attention; do not auto-upgrade the database")
        except (HarnessError, OSError) as exc:
            if "migration" in str(exc):
                raise
            last = str(exc)
        time.sleep(3)
    raise HarnessError(f"SonarQube did not become ready: {last}")


def secret_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    path.chmod(0o600)


def _admin_password_path(root: Path) -> Path:
    return root / ".local" / "sonar-admin.password"


SONAR_READER_LOGIN = "local-harness-reader"


def _valid_login_client(url: str, login: str, password: str) -> Sonar | None:
    """Return an authenticated local client, without leaking credentials."""
    client = Sonar(url, login=login, password=password)
    try:
        if client.call("authentication/validate").get("valid"):
            return client
    except HarnessError:
        # Invalid credentials are handled by the fallback prompt. The original
        # exception may contain server response text and is not useful here.
        pass
    return None


def _valid_admin_client(url: str, login: str, password: str) -> Sonar | None:
    """Return an authenticated admin client, without leaking credentials."""
    return _valid_login_client(url, login, password)


def _admin_client(root: Path, url: str) -> Sonar:
    """Authenticate Sonar for bootstrap and auto-rotate the fresh default login.

    A new local Sonar instance starts with admin/admin. The first bootstrap
    rotates that credential through the API and stores the generated password in
    a mode-0600 file outside project repositories. Existing installations use
    that file, explicit environment credentials, or the guarded interactive
    fallback. No admin credential enters reports or the project profile.
    """
    login = os.environ.get("SONAR_ADMIN_LOGIN", "admin").strip() or "admin"
    password_path = _admin_password_path(root)
    stored_password = ""
    if password_path.is_file():
        stored_password = password_path.read_text(encoding="utf-8").strip()

    candidates: list[tuple[str, str]] = []
    supplied_password = os.environ.get("SONAR_ADMIN_PASSWORD", "").strip()
    if supplied_password:
        candidates.append((login, supplied_password))
    if stored_password and (login, stored_password) not in candidates:
        candidates.append((login, stored_password))
    if login == "admin" and (login, "admin") not in candidates:
        candidates.append((login, "admin"))

    for candidate_login, candidate_password in candidates:
        client = _valid_admin_client(url, candidate_login, candidate_password)
        if client is None:
            continue
        if candidate_login == "admin" and candidate_password == "admin":
            generated_password = _new_local_password()
            client.post("users/change_password", login="admin",
                        previousPassword="admin", password=generated_password)
            rotated = _valid_admin_client(url, "admin", generated_password)
            if rotated is None:
                raise HarnessError(
                    "SonarQube accepted automatic admin password rotation, "
                    "but the generated credential could not be verified"
                )
            secret_file(password_path, generated_password)
            print(f"Detected fresh SonarQube login; generated local admin credential at {password_path}")
            return rotated
        return client

    try:
        prompt_login = input(f"Admin login [{login}]: ").strip() or login
        prompt_password = getpass.getpass("Admin password (not echoed): ")
    except EOFError as exc:
        raise HarnessError(
            "No usable Sonar admin credential found. Set SONAR_ADMIN_PASSWORD or "
            f"provide the password interactively; expected local secret file: {password_path}"
        ) from exc
    client = _valid_admin_client(url, prompt_login, prompt_password)
    if client is None:
        raise HarnessError("SonarQube authentication failed")
    return client


_SONAR_KEY = re.compile(r"[A-Za-z0-9_.:-]+$")
_SONAR_LANGUAGES = ("java", "js", "ts")
_IGNORED_LANGUAGE_DIRS = {".git", "node_modules", "target", ".next", "coverage",
                          "test-results", "playwright-report", ".local"}


def _safe_sonar_key(value: str, label: str = "Sonar project key") -> str:
    if not isinstance(value, str) or not value or not _SONAR_KEY.fullmatch(value):
        raise HarnessError(f"{label} must contain only letters, digits, '.', '_' ':' or '-'")
    return value


def _reader_password_path(root: Path) -> Path:
    return root / ".local" / "sonar-reader.password"


def _new_local_password() -> str:
    # Sonar's default password policy requires a special character.
    return secrets.token_urlsafe(32) + "!Aa1"


def _profile_languages(profile: dict, repo: Path) -> list[str]:
    """Resolve supported Sonar languages without trusting project commands."""
    explicit = profile.get("sonar_languages")
    if explicit is not None:
        if not isinstance(explicit, list) or not explicit or any(
            language not in _SONAR_LANGUAGES for language in explicit
        ):
            raise HarnessError("sonar_languages must contain only java, js and ts")
        return [language for language in _SONAR_LANGUAGES if language in explicit]

    adapter = profile["project"]["adapter"]
    if adapter == "spring-maven":
        return ["java"]
    if adapter in {"next-npm", "next-fullstack"}:
        return ["js", "ts"]

    app = repo / Path(profile["project"]["subdir"])
    languages: set[str] = set()
    if (app / "pom.xml").is_file():
        languages.add("java")
    if (app / "package.json").is_file():
        languages.update(("js", "ts"))
    if not languages:
        for path in app.rglob("*"):
            if not path.is_file() or path.is_symlink() or any(
                part in _IGNORED_LANGUAGE_DIRS for part in path.relative_to(app).parts
            ):
                continue
            if path.suffix == ".java":
                languages.add("java")
            elif path.suffix in {".js", ".jsx", ".mjs", ".cjs"}:
                languages.add("js")
            elif path.suffix in {".ts", ".tsx", ".mts", ".cts"}:
                languages.add("ts")

    result = [language for language in _SONAR_LANGUAGES if language in languages]
    if not result:
        raise HarnessError(
            "Cannot derive a supported Sonar language from the project adapter; "
            "set sonar_languages to java, js and/or ts"
        )
    return result


def _profile_spec(repo: Path, profile: dict) -> dict[str, Any]:
    from project import project_slug

    slug = project_slug(profile, repo)
    key = _safe_sonar_key(profile.get("sonar_key") or slug)
    return {
        "id": slug,
        "project_key": key,
        "name": profile["project"]["name"],
        "adapter": profile["project"]["adapter"],
        "languages": _profile_languages(profile, repo),
        "target_branch": profile["target_branch"],
        "new_code": {"type": "REFERENCE_BRANCH", "value": profile["target_branch"]},
        "token_file": f".local/sonar-{slug}.token",
        "profile_path": ".ci/harness.json",
    }


def _legacy_specs(config: dict) -> list[dict[str, Any]]:
    projects = config.get("projects")
    if not isinstance(projects, dict) or not projects:
        raise HarnessError("No legacy projects configured; pass --repo with a project profile")
    specs = []
    for target, project in projects.items():
        key = _safe_sonar_key(project["sonar_key"], f"{target}.sonar_key")
        specs.append({
            "id": target,
            "project_key": key,
            "name": key,
            "adapter": "spring-maven" if target == "backend" else "next-npm",
            "languages": ["java"] if target == "backend" else ["js", "ts"],
            "target_branch": None,
            "new_code": {"type": "NUMBER_OF_DAYS", "value": str(config["sonar"].get("new_code_days", 30))},
            "token_file": f".local/sonar-{target}.token",
            "profile_path": None,
        })
    return specs


def _configure_quality_gate(client: Sonar, config: dict) -> tuple[dict, list[tuple[str, str, str]], bool]:
    gate_name = "Local Harness - Practical v1"
    gates = client.call("qualitygates/list").get("qualitygates", [])
    gate = next((g for g in gates if g["name"] == gate_name), None)
    if gate is None:
        gate = client.post("qualitygates/create", name=gate_name)
    gate = dict(gate, name=gate_name)
    existing = client.call("qualitygates/show", {"name": gate_name})
    # Only this harness-owned gate is modified. Never alter Sonar way or unrelated gates.
    for condition in existing.get("conditions", []):
        client.post("qualitygates/delete_condition", id=condition["id"])
    metrics = set()
    page = 1
    while True:
        payload = client.call("metrics/search", {"ps": 500, "p": page})
        entries = payload.get("metrics", [])
        metrics.update(m["key"] for m in entries)
        if len(entries) < 500:
            break
        page += 1
    mode = client.call("settings/values", {"keys": "sonar.multi-quality-mode.enabled"})
    mqr = any(s.get("value") == "true" for s in mode.get("settings", []))
    bug_metric = "new_software_quality_reliability_issues" if mqr else "new_bugs"
    security_metric = "new_software_quality_security_issues" if mqr else "new_vulnerabilities"
    required = {bug_metric, security_metric, "new_coverage"}
    if not required.issubset(metrics):
        raise HarnessError(f"Server does not expose expected gate metrics: {required - metrics}. Review mode/API; no weakened fallback applied")
    conditions = [(bug_metric, "GT", "0"), (security_metric, "GT", "0"),
                  ("new_coverage", "LT", str(config["sonar"]["new_code_coverage"]))]
    # Newer Sonar versions phase out hotspots; security issues remain mandatory.
    if "new_security_hotspots_reviewed" in metrics:
        conditions.append(("new_security_hotspots_reviewed", "LT", "100"))
    for metric, op, threshold in conditions:
        client.gate_post("qualitygates/create_condition", gate, metric=metric, op=op, error=threshold)
    return gate, conditions, mqr


def _ensure_reader(client: Sonar, root: Path) -> str:
    """Create a least-privilege reader and persist its UI password locally."""
    reader_login = SONAR_READER_LOGIN
    password_path = _reader_password_path(root)
    stored_password = password_path.read_text(encoding="utf-8").strip() if password_path.is_file() else ""
    found = client.call("users/search", {"q": reader_login}).get("users", [])
    existing = next((user for user in found if user.get("login") == reader_login), None)
    if existing is not None and existing.get("local", True) is False:
        raise HarnessError(
            f"Sonar reader login {reader_login!r} is managed by an external identity provider; "
            "choose a different local reader before provisioning"
        )
    if existing is None:
        stored_password = _new_local_password()
        client.post("users/create", login=reader_login, name="Local Harness Reader",
                    password=stored_password, local="true")
        secret_file(password_path, stored_password)
    elif not stored_password:
        # Older harness runs created this user only for API tokens and did not
        # retain a browser password. Reset it through the admin client so the
        # read-only UI login becomes usable without exposing admin credentials.
        stored_password = _new_local_password()
        client.post("users/change_password", login=reader_login, password=stored_password)
        secret_file(password_path, stored_password)
    groups = client.call("users/groups", {"login": reader_login, "ps": 500}).get("groups", [])
    # Sonar assigns every local user to its built-in `sonar-users` group and
    # rejects attempts to remove that membership. It does not grant private
    # project browse access by itself; only remove additional memberships that
    # the API permits us to remove.
    for group in groups:
        if group.get("selected") and group.get("name") not in ("Anyone", "sonar-users"):
            client.post("user_groups/remove_user", name=group["name"], login=reader_login)
    token_name = "harness-reader-" + secrets.token_hex(4)
    token = client.post("user_tokens/generate", login=reader_login, name=token_name, type="USER_TOKEN")
    secret_file(root / ".local" / "sonar-reader.token", token["token"])
    return reader_login


def print_ui_credentials(root: Path, url: str) -> None:
    """Print the intentionally requested, least-privilege Sonar UI credentials."""
    password_path = _reader_password_path(root)
    if not password_path.is_file():
        raise HarnessError(
            "No Sonar UI credentials found. Run ./ci up and ./ci bootstrap first."
        )
    password = password_path.read_text(encoding="utf-8").strip()
    if not password:
        raise HarnessError(f"Sonar UI credential file is empty: {password_path}")
    print("SonarQube UI (read-only)")
    print(f"URL: {url}")
    print(f"Username: {SONAR_READER_LOGIN}")
    print(f"Password: {password}")
    print("This command intentionally prints a local secret; do not paste it into logs or reports.")


def _ensure_quality_profile(client: Sonar, language: str, project_key: str) -> dict[str, Any]:
    desired_name = "Local Harness - " + language
    profiles = client.call("qualityprofiles/search", {"language": language}).get("profiles", [])
    desired = next((p for p in profiles if p["name"] == desired_name), None)
    if desired is None:
        base = next((p for p in profiles if p["name"] == "Sonar way" and p.get("isBuiltIn")), None)
        if base is None:
            raise HarnessError(f"No built-in Sonar way profile for {language}; check installed language analyzers")
        result = client.post("qualityprofiles/copy", **{"fromKey": base["key"], "toName": desired_name})
        desired = result.get("profile", result)
    client.post("qualityprofiles/add_project", language=language,
                qualityProfile=desired_name, project=project_key)
    return {"name": desired_name, "key": desired.get("key"),
            "active_rules": desired.get("activeRuleCount")}


def _provision_project(client: Sonar, root: Path, gate: dict, reader_login: str,
                       spec: dict[str, Any], manifest: dict[str, Any]) -> None:
    key = spec["project_key"]
    projects = client.call("projects/search", {"projects": key}).get("components", [])
    if not projects:
        client.post("projects/create", project=key, name=spec["name"], visibility="private")
    client.gate_post("qualitygates/select", gate, projectKey=key)
    new_code = spec.get("new_code")
    if new_code:
        client.post("new_code_periods/set", project=key, type=new_code["type"], value=new_code["value"])
    client.post("settings/set", component=key, key="sonar.qualitygate.ignoreSmallChanges", value="false")
    for permission in ("user", "codeviewer"):
        client.post("permissions/add_user", login=reader_login, projectKey=key, permission=permission)
    for language in spec["languages"]:
        manifest["profiles"][language] = _ensure_quality_profile(client, language, key)
    analysis = client.post("user_tokens/generate", name=f"harness-{spec['id']}-{secrets.token_hex(4)}",
                           type="PROJECT_ANALYSIS_TOKEN", projectKey=key)
    secret_file(root / spec["token_file"], analysis["token"])
    manifest["projects"][spec["id"]] = key
    if spec.get("profile_path"):
        manifest["profile_projects"][spec["id"]] = {
            "project_key": key,
            "adapter": spec["adapter"],
            "languages": spec["languages"],
            "target_branch": spec["target_branch"],
            "new_code": dict(spec["new_code"]),
            "profile_path": spec["profile_path"],
            "token_file": spec["token_file"],
        }


def provision(root: Path, config: dict, url: str, profile_repo: Path | None = None) -> None:
    wait_ready(url)
    profile = None
    if profile_repo is not None:
        from project import load_profile

        profile_repo = Path(profile_repo).expanduser().resolve()
        if not profile_repo.is_dir():
            raise HarnessError(f"Profile repository not found: {profile_repo}")
        profile, _ = load_profile(profile_repo)
    client = _admin_client(root, url)
    # Invalidate the success marker BEFORE any mutation. A failed/partial
    # bootstrap must not leave old tokens looking like a usable policy.
    policy_path = root / ".local" / "sonar-policy.json"
    prior = read_json(policy_path) if policy_path.exists() else {}
    if not isinstance(prior, dict):
        raise HarnessError("Existing Sonar policy manifest is not a JSON object")
    policy_path.unlink(missing_ok=True)
    gate, conditions, mqr = _configure_quality_gate(client, config)
    manifest = {
        **prior,
        "gate": gate["name"],
        "mqr": mqr,
        "conditions": conditions,
        "duplication": "report-only",
        "maintainability": "report-only",
        "profiles": dict(prior.get("profiles", {})),
        "projects": dict(prior.get("projects", {})),
        "profile_projects": dict(prior.get("profile_projects", {})),
        "provisioned": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    reader_login = _ensure_reader(client, root)
    specs = [_profile_spec(profile_repo, profile)] if profile is not None else _legacy_specs(config)
    for spec in specs:
        _provision_project(client, root, gate, reader_login, spec, manifest)
    manifest["server_gate"] = client.call("qualitygates/show", {"name": gate["name"]})
    write_json(root / ".local" / "sonar-policy.json", manifest)
    if profile is not None:
        print(f"Provisioned project {specs[0]['project_key']} from {profile_repo / '.ci/harness.json'}.")
    else:
        print("Provisioned legacy backend/frontend projects.")
    print("Provisioned local projects, profiles, gate and scoped tokens. Tokens and the read-only UI password are stored under .local (0600).")
    print("Sonar UI: " + url + " (read-only credentials: ./ci sonar credentials)")
    print("Re-provisioning creates new tokens; revoke superseded tokens in the UI. Review .local/sonar-policy.json.")


def export_analysis(root: Path, report_dir: Path, task_file: Path, key: str, url: str) -> dict:
    if not task_file.exists():
        raise HarnessError(f"No report-task.txt from this run: {task_file}")
    properties = dict(line.split("=", 1) for line in task_file.read_text().splitlines() if "=" in line)
    task_id = properties.get("ceTaskId")
    if not task_id:
        raise HarnessError("Sonar report-task.txt contains no compute-engine task ID")
    client = Sonar(url, token=(root / ".local" / "sonar-reader.token").read_text().strip())
    deadline = time.monotonic() + 600
    task = {}
    while time.monotonic() < deadline:
        task = client.call("ce/task", {"id": task_id}).get("task", {})
        if task.get("status") in ("SUCCESS", "FAILED", "CANCELED"):
            break
        time.sleep(2)
    write_json(report_dir / "sonar-task.json", task)
    if task.get("status") != "SUCCESS" or not task.get("analysisId"):
        raise HarnessError(f"Sonar background analysis did not succeed: {task.get('status')}")
    gate = client.call("qualitygates/project_status", {"analysisId": task["analysisId"]})
    write_json(report_dir / "sonar-quality-gate.json", gate)
    issues, total = [], 0
    for page in range(1, 21):
        payload = client.call("issues/search", {"componentKeys": key, "resolved": "false", "ps": 500, "p": page})
        issues.extend(payload.get("issues", []))
        total = payload.get("paging", {}).get("total", payload.get("total", len(issues)))
        if len(issues) >= total:
            break
    write_json(report_dir / "sonar-issues.json", {"issues": issues, "total": total, "truncated": len(issues) < total})
    status = gate.get("projectStatus", {}).get("status")
    if status != "OK":
        raise HarnessError(f"Sonar quality gate is {status}; see sonar-quality-gate.json and sonar-issues.json")
    return {"analysis_id": task["analysisId"], "quality_gate": status, "open_issues": total}
