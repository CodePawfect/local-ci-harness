"""Execution, snapshots and evidence. No third-party Python dependencies."""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Any
import xml.etree.ElementTree as ET


class HarnessError(RuntimeError):
    """Configuration, infrastructure or verification error; never a passing check."""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def capture(args: list[str], cwd: Path | None = None, timeout: int = 120,
            env: dict[str, str] | None = None) -> str:
    try:
        p = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"Cannot execute {args[0]}: {exc}") from exc
    if p.returncode:
        raise HarnessError(f"{args[0]} exited {p.returncode}: {p.stderr[-3000:]}")
    return p.stdout.strip()


def git(root: Path, *args: str) -> str:
    return capture(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args])


def safe_relative(value: str) -> Path:
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise HarnessError(f"Expected a relative path without '..': {value}")
    return p


EXCLUDED_DIRS = {"node_modules", "target", ".next", ".git", "coverage", "playwright-report",
                 "test-results", "__pycache__", ".local"}


def source_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise HarnessError(f"Repository not found: {root}")
    actual = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    if actual != root.resolve():
        raise HarnessError(f"path must be the Git repository root ({actual}); use subdir for the application")
    if (root / ".gitmodules").exists():
        raise HarnessError("Git submodules need explicit snapshot support; this starter deliberately refuses to omit them")
    # Tracked modifications + non-ignored untracked files; never only HEAD.
    raw = capture(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    paths = []
    for name in sorted(set(raw.split("\0"))):
        if not name:
            continue
        rel = safe_relative(name)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        p = root / rel
        if p.is_symlink():
            try:
                p.resolve(strict=True).relative_to(root.resolve())
            except (OSError, ValueError) as exc:
                raise HarnessError(f"External or dangling symlink refused: {rel}") from exc
        if p.exists() and (p.is_file() or p.is_symlink()):
            paths.append(rel)
    if not paths:
        raise HarnessError(f"No source files found in {root}")
    return paths


def tree_hash(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        p = root / rel
        digest.update(str(rel).encode("utf-8", "surrogateescape") + b"\0")
        if p.is_symlink():
            digest.update(b"symlink\0" + os.readlink(p).encode())
        else:
            digest.update(p.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclasses.dataclass
class Snapshot:
    source: Path
    root: Path
    app: Path
    source_hash: str
    commit: str
    files: list[Path]


def snapshot(source: Path, destination: Path, subdir: str, history: bool) -> Snapshot:
    files = source_files(source)
    before = tree_hash(source, files)
    commit = git(source, "rev-parse", "HEAD")
    if history:
        # Local Git transport also works for linked worktrees. No alternates pointing
        # outside the snapshot, no dependency on host .git absolute paths in containers.
        capture(["git", "-c", "core.hooksPath=/dev/null", "-c", "init.templateDir=",
                 "clone", "--quiet", "--no-checkout", "--no-local", str(source), str(destination)], timeout=300)
        git(destination, "reset", "--mixed", commit)
        git(destination, "config", "core.hooksPath", "/dev/null")
    else:
        destination.mkdir(parents=True)
    for rel in files:
        src, dst = source / rel, destination / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            # Rewrite absolute internal links to relative links inside the snapshot.
            target_rel = src.resolve().relative_to(source.resolve())
            dst.symlink_to(os.path.relpath(destination / target_rel, dst.parent))
        else:
            shutil.copy2(src, dst)
    if files != source_files(source) or before != tree_hash(source, files):
        raise HarnessError("Source changed while copying. Stop editing and rerun the pipeline")
    app = destination / safe_relative(subdir)
    if not app.is_dir():
        raise HarnessError(f"Application subdir does not exist: {subdir}")
    return Snapshot(source, destination, app, before, commit, files)


def verify_unchanged(snap: Snapshot) -> None:
    files = source_files(snap.source)
    if files != snap.files or tree_hash(snap.source, files) != snap.source_hash:
        raise HarnessError("Working tree changed after the snapshot. This result does not verify the current source")


def require_junit(directory: Path) -> dict[str, int]:
    paths = sorted(directory.glob("TEST-*.xml"))
    if not paths:
        raise HarnessError(f"No JUnit XML reports in {directory}. Tests may be absent or skipped")
    result = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for path in paths:
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        for suite in suites:
            for key in result:
                result[key] += int(suite.attrib.get(key, "0"))
    if result["tests"] - result["skipped"] <= 0:
        raise HarnessError(f"No executed tests in {directory}")
    if result["failures"] or result["errors"]:
        raise HarnessError(f"JUnit reports contain failures/errors: {result}")
    return result


def jacoco_coverage(path: Path, line_min: float, branch_min: float) -> dict[str, float | None]:
    if not path.exists():
        raise HarnessError(f"Missing JaCoCo XML: {path}")
    root = ET.parse(path).getroot()
    values: dict[str, float | None] = {}
    for typ, key, threshold in [("LINE", "lines", line_min), ("BRANCH", "branches", branch_min)]:
        counter = root.find(f"counter[@type='{typ}']")
        if counter is None:
            if typ == "LINE":
                raise HarnessError("JaCoCo report contains no LINE counter")
            values[key] = None
            continue
        covered = int(counter.attrib["covered"])
        total = covered + int(counter.attrib["missed"])
        if total == 0:
            if typ == "LINE":
                raise HarnessError("JaCoCo report has zero executable source lines")
            values[key] = None
            continue
        pct = 100 * covered / total
        values[key] = round(pct, 2)
        if pct < threshold:
            raise HarnessError(f"{key} coverage {pct:.2f}% < {threshold}%")
    return values


def frontend_coverage(path: Path, line_min: float, branch_min: float) -> dict[str, Any]:
    data = read_json(path)
    total = data.get("total", {})
    result = {}
    for key, threshold in [("lines", line_min), ("branches", branch_min)]:
        item = total.get(key)
        if not isinstance(item, dict) or "total" not in item or "covered" not in item:
            raise HarnessError(f"Malformed coverage-summary.json: missing {key}")
        n, covered = item["total"], item["covered"]
        if not isinstance(n, (int, float)) or not isinstance(covered, (int, float)) or n < 0 or not 0 <= covered <= n:
            raise HarnessError(f"Invalid {key} coverage counters")
        if n == 0:
            if key == "lines":
                raise HarnessError("Frontend coverage has zero executable lines")
            result[key] = None
        else:
            pct = 100 * covered / n
            result[key] = round(pct, 2)
            if pct < threshold:
                raise HarnessError(f"{key} coverage {pct:.2f}% < {threshold}%")
    return result


def validate_semgrep(path: Path) -> dict[str, Any]:
    data = read_json(path)
    # Parse failures/timeouts cannot silently turn into a clean security result.
    if data.get("errors"):
        raise HarnessError(f"Semgrep returned {len(data['errors'])} analysis errors; see the report")
    results = data.get("results")
    if not isinstance(results, list):
        raise HarnessError("Malformed Semgrep report")
    scanned = data.get("paths", {}).get("scanned", [])
    if not isinstance(scanned, list) or not scanned:
        raise HarnessError("Semgrep scanned no source files; a zero-target scan is not a pass")
    counts = {"ERROR": 0, "WARNING": 0, "INFO": 0}
    for finding in results:
        severity = finding.get("extra", {}).get("severity", "ERROR")
        counts[severity] = counts.get(severity, 0) + 1
    if counts["ERROR"]:
        raise HarnessError(f"Semgrep found {counts['ERROR']} blocking issues")
    return dict(counts, scanned_files=len(scanned), _status="WARN" if counts["WARNING"] else "PASS")


def validate_trivy(path: Path, need_packages: bool = True) -> dict[str, int]:
    data = read_json(path)
    if not isinstance(data.get("Results"), list):
        if need_packages:
            raise HarnessError("Trivy did not return dependency results")
        return {"packages": 0, "blocking_findings": 0}
    packages = sum(len(result.get("Packages", [])) for result in data["Results"])
    blocking = sum(1 for result in data["Results"] for field in ("Vulnerabilities", "Misconfigurations")
                   for finding in result.get(field, []) if finding.get("Severity") in ("HIGH", "CRITICAL")
                   and finding.get("Status", "FAIL") != "PASS")
    if need_packages and packages == 0:
        raise HarnessError("Trivy identified zero packages. A scan without dependencies is not a pass")
    if blocking:
        raise HarnessError(f"Trivy found {blocking} HIGH/CRITICAL findings")
    return {"packages": packages, "blocking_findings": blocking}


@dataclasses.dataclass
class Result:
    name: str
    status: str
    seconds: float
    exit_code: int | None = None
    log: str | None = None
    details: Any = None


class Runner:
    def __init__(self, root: Path, config: dict[str, Any], mode: str, target: str,
                 namespace: str | None = None, metadata: dict[str, Any] | None = None):
        self.root, self.config = root, config
        self.namespace = namespace
        self.id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        runtime_root = root / ".local" / "projects" / namespace if namespace else root / ".local"
        report_root = root / "reports" / namespace if namespace else root / "reports"
        self.work = runtime_root / "runs" / self.id
        self.reports = report_root / self.id
        self.latest = report_root / "latest"
        self.work.mkdir(parents=True)
        self.reports.mkdir(parents=True)
        self.results: list[Result] = []
        self.meta: dict[str, Any] = {"run_id": self.id, "mode": mode, "target": target,
                                    "snapshots": {}, "warning": "No complete OWASP or production security certification"}
        if metadata:
            self.meta.update(metadata)
        self.lock = read_json(root / "images.lock.json")
        self.meta["images"] = self.lock
        self.flush()

    def image(self, name: str) -> str:
        item = self.lock.get("images", {}).get(name)
        if not item or not re.search(r"@sha256:[0-9a-f]{64}$", item.get("digest", "")):
            raise HarnessError(f"No locked digest for {name}. Run ./ci lock-images (or --runtime for Playwright/ZAP)")
        return item["digest"]

    def flush(self) -> None:
        failures = [r for r in self.results if r.status in ("FAIL", "ERROR", "BLOCKED")]
        summary = dict(self.meta, status="FAIL" if failures else "RUNNING",
                       steps=[dataclasses.asdict(r) for r in self.results])
        write_json(self.reports / "summary.json", summary)
        lines = [f"# Local CI — {self.id}", "", f"Mode: `{self.meta['mode']}`; target: `{self.meta['target']}`", "",
                 "| Check | Status | Seconds |", "|---|---|---:|"]
        lines.extend(f"| {r.name} | {r.status} | {r.seconds:.1f} |" for r in self.results)
        lines += ["", "Read summary.json and the individual logs/reports. A partial/quick run is not a release approval.", ""]
        (self.reports / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    def check(self, name: str, fn: Callable[[], Any]) -> bool:
        start = time.monotonic()
        print(f"\n>>> {name}", flush=True)
        try:
            detail = fn()
            state = detail.pop("_status", "PASS") if isinstance(detail, dict) else "PASS"
            if state not in ("PASS", "WARN"):
                raise HarnessError("Invalid evidence status")
            item = Result(name, state, time.monotonic() - start, details=detail)
        except (HarnessError, OSError, ValueError, ET.ParseError, KeyError) as exc:
            item = Result(name, "FAIL", time.monotonic() - start, details=str(exc))
        self.results.append(item)
        print(f"{item.status}: {name}" + (f" — {item.details}" if item.status != "PASS" else ""), flush=True)
        self.flush()
        return item.status in ("PASS", "WARN")

    def blocked(self, name: str, reason: str) -> None:
        self.results.append(Result(name, "BLOCKED", 0, details=reason))
        self.flush()

    def docker(self, name: str, image: str, args: list[str], snap: Snapshot | None = None,
               *, entrypoint: str | None = None, network: str = "bridge", env: dict[str, str] | None = None,
               readonly_source: bool = False, mounts: list[tuple[Path, str, bool]] | None = None,
               allowed_codes: tuple[int, ...] = (0,), rootfs_readonly: bool = True,
               host_gateway: bool = False) -> bool:
        start = time.monotonic()
        logfile = self.reports / f"{name}.log"
        cname = f"lci-{self.id.lower()}-{name.lower()}"[:120]
        uid, gid = os.getuid(), os.getgid()
        command = ["docker", "run", "--rm", "--init", "--name", cname, "--user", f"{uid}:{gid}",
                   "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit=512",
                   "--memory", self.config.get("container_memory", "4g"), "--network", network,
                   "--shm-size=1g", "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g,mode=1777"]
        if host_gateway:
            command += ["--add-host=host.docker.internal:host-gateway"]
        if rootfs_readonly:
            command.append("--read-only")
        merged_env = {"HOME": "/tmp", "CI": "true", "NEXT_TELEMETRY_DISABLED": "1", "SEMGREP_SEND_METRICS": "off",
                      "SEMGREP_ENABLE_VERSION_CHECK": "0", "TRIVY_DISABLE_TELEMETRY": "true"}
        if env:
            merged_env.update(env)
        child_env = dict(os.environ)
        child_env.update(merged_env)
        # Values (including tokens) are passed through the process environment, not argv.
        for key in merged_env:
            command += ["--env", key]
        bindings = [(self.root / "policy", "/policy", True), (self.reports, "/reports", False)]
        if snap:
            bindings += [(snap.root, "/workspace", readonly_source)]
            rel = snap.app.relative_to(snap.root).as_posix()
            command += ["--workdir", "/workspace" + ("/" + rel if rel != "." else "")]
        if mounts:
            bindings += mounts
        for host, target, ro in bindings:
            # Docker --mount treats comma as a delimiter; refuse instead of mis-mounting.
            if "," in str(host):
                raise HarnessError(f"Docker bind paths cannot contain commas: {host}")
            command += ["--mount", f"type=bind,src={host.resolve()},dst={target}" + (",readonly" if ro else "")]
        if entrypoint:
            command += ["--entrypoint", entrypoint]
        command += [self.image(image), *args]
        print(f"\n>>> {name} (log: {logfile.relative_to(self.root)})", flush=True)
        code, status, detail = None, "ERROR", None
        proc = None
        try:
            with logfile.open("w", encoding="utf-8") as log:
                proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=child_env)
                try:
                    code = proc.wait(timeout=int(self.config.get("step_timeout_seconds", 1200)))
                    status = "PASS" if code in allowed_codes else "FAIL"
                    if code in (125, 126, 127):
                        status = "ERROR"
                    if code == 2 and 2 in allowed_codes:
                        status = "WARN"
                except subprocess.TimeoutExpired:
                    detail = "Container step timed out (not a successful scan)"
                    status = "ERROR"
        except OSError as exc:
            detail = str(exc)
        finally:
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    subprocess.run(["docker", "rm", "-f", cname], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=False, timeout=30)
                proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=10)
        # Defense in depth: redact only known credentials; arbitrary app logs can still
        # contain sensitive data. Reports must never be published without review.
        if logfile.exists():
            text = logfile.read_text(encoding="utf-8", errors="replace")
            for key, value in merged_env.items():
                if any(s in key.upper() for s in ("TOKEN", "PASSWORD", "SECRET")) and len(value) >= 6:
                    text = text.replace(value, "[REDACTED]")
            logfile.write_text(text, encoding="utf-8")
        result = Result(name, status, round(time.monotonic() - start, 3), code,
                        str(logfile.relative_to(self.root)), detail)
        self.results.append(result)
        print(f"{status}: {name}", flush=True)
        if status in ("FAIL", "ERROR") and logfile.exists():
            print("\n".join(logfile.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]), flush=True)
        self.flush()
        return status in ("PASS", "WARN")

    def finish(self) -> int:
        self.flush()
        summary_path = self.reports / "summary.json"
        summary = read_json(summary_path)
        summary["status"] = "FAIL" if any(r.status in ("FAIL", "ERROR", "BLOCKED") for r in self.results) else "PASS"
        summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        summary["warning_count"] = sum(r.status == "WARN" for r in self.results)
        if "gate" in self.meta:
            summary["gate"] = self.meta["gate"]
        write_json(summary_path, summary)
        markdown = self.reports / "summary.md"
        gate_line = f"; gate: **{summary['gate']['status']}**" if "gate" in summary else ""
        markdown.write_text(markdown.read_text(encoding="utf-8") +
                            f"\nOverall: **{summary['status']}**; warnings: {summary['warning_count']}{gate_line}\n",
                            encoding="utf-8")
        latest = self.latest
        if latest.is_symlink():
            latest.unlink()
        elif latest.exists():
            raise HarnessError("reports/latest exists and is not a symlink; refusing to overwrite")
        latest.symlink_to(self.reports.name, target_is_directory=True)
        print(f"\n{summary['status']} — {self.reports / 'summary.md'}", flush=True)
        return 0 if summary["status"] == "PASS" else 1
