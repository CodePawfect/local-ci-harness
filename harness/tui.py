"""Small standard-library setup TUI.

The terminal renderer is intentionally thin. Profile construction and
validation live in project.py so they can be tested without a real terminal.
"""
from __future__ import annotations

import contextlib
from typing import Iterable

from core import HarnessError
from project import Detection, STAGES, profile_from_detection, profile_issues


ADAPTER_LABELS = {
    "generic": "Generic Git repository",
    "next-npm": "Next.js / npm",
    "next-fullstack": "Next.js Fullstack",
    "spring-maven": "Spring Boot / Maven",
    "custom": "Custom commands",
}
KIND_LABELS = {
    "frontend": "Frontend",
    "backend": "Backend",
    "next-fullstack": "Next.js Fullstack",
    "custom": "Custom",
}


def _adapter_options(kind: str) -> list[str]:
    return {
        "frontend": ["next-npm", "custom", "generic"],
        "backend": ["spring-maven", "custom", "generic"],
        "next-fullstack": ["next-fullstack"],
        "custom": ["custom", "generic"],
    }[kind]


def _menu(stdscr, title: str, options: list[str], selected: int = 0,
          multi: bool = False, initial: set[int] | None = None):
    import curses

    cursor = max(0, min(selected, len(options) - 1))
    chosen = set(initial or set())
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, title, max(1, width - 1), curses.A_BOLD)
        help_text = "↑↓ move · Enter confirm"
        if multi:
            help_text = "↑↓ move · Space toggle · Enter confirm"
        stdscr.addnstr(1, 0, help_text, max(1, width - 1), curses.A_DIM)
        for index, option in enumerate(options):
            if 3 + index >= height - 1:
                break
            prefix = "[x] " if multi and index in chosen else "[ ] " if multi else "  "
            text = prefix + option
            attr = curses.A_REVERSE if index == cursor else curses.A_NORMAL
            stdscr.addnstr(3 + index, 0, text, max(1, width - 1), attr)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % len(options)
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % len(options)
        elif multi and key == ord(" "):
            if cursor in chosen:
                chosen.remove(cursor)
            else:
                chosen.add(cursor)
        elif key in (10, 13, curses.KEY_ENTER):
            return chosen if multi else cursor
        elif key in (27, ord("q")):
            raise HarnessError("Setup cancelled by user")


def _confirm(stdscr, message: str) -> bool:
    import curses

    height, width = stdscr.getmaxyx()
    stdscr.erase()
    stdscr.addnstr(0, 0, message, max(1, width - 1), curses.A_BOLD)
    stdscr.addnstr(2, 0, "y confirm · n cancel", max(1, width - 1), curses.A_DIM)
    stdscr.refresh()
    while True:
        key = stdscr.getch()
        if key in (ord("y"), ord("Y"), 10, 13):
            return True
        if key in (ord("n"), ord("N"), 27, ord("q")):
            return False


def _session(stdscr, detection: Detection) -> tuple[dict, bool]:
    import curses

    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    kinds = list(KIND_LABELS)
    default_kind = kinds.index(detection.kind) if detection.kind in kinds else kinds.index("custom")
    kind_index = _menu(
        stdscr,
        f"Project type for {detection.repo}",
        [f"{name}: {KIND_LABELS[name]}" for name in kinds],
        selected=default_kind,
    )
    kind = kinds[kind_index]
    adapters = _adapter_options(kind)
    default_adapter = adapters.index(detection.adapter) if detection.adapter in adapters else 0
    adapter_index = _menu(
        stdscr,
        "Select framework / adapter",
        [f"{name}: {ADAPTER_LABELS[name]}" for name in adapters],
        selected=default_adapter,
    )
    adapter = adapters[adapter_index]
    stage_indices = _menu(
        stdscr,
        "Select CI stages",
        list(STAGES),
        multi=True,
        initial={STAGES.index(stage) for stage in detection.stages if stage in STAGES},
    )
    stages = [STAGES[index] for index in sorted(stage_indices)]
    if not stages:
        raise HarnessError("At least one CI stage must be selected")
    profile = profile_from_detection(detection, adapter=adapter, stages=stages, kind=kind)
    issues = profile_issues(profile)
    summary = f"Adapter: {adapter}; Target branch: {profile['target_branch']}; Stages: {', '.join(stages)}"
    if issues:
        summary += " | Notes: " + " / ".join(issues)
    if not _confirm(stdscr, summary):
        raise HarnessError("Setup cancelled by user")
    run_now = _confirm(stdscr, "Profile saved. Prepare an initial gate run now?")
    return profile, run_now


def run_setup_tui(detection: Detection) -> tuple[dict, bool]:
    try:
        import curses
    except ImportError as exc:
        raise HarnessError("Python curses is required for interactive setup on this platform") from exc
    return curses.wrapper(lambda stdscr: _session(stdscr, detection))


def format_detection(detection: Detection) -> str:
    scripts = ", ".join(detection.available_scripts) or "none"
    files = ", ".join(detection.available_files) or "none"
    hints = "; ".join(getattr(detection, "hints", [])) or "none"
    return f"Detected kind={detection.kind}, adapter={detection.adapter}, target={detection.target_branch}, scripts={scripts}, files={files}, hints={hints}"
