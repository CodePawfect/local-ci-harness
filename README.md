# Local CI Harness

> Local-first, policy-controlled CI for existing Git repositories.

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20WSL2-0f766e.svg)](#platforms)

Run reproducible security, quality, test and build gates locally before a merge or
push. No GitHub, Jenkins or CI server is required. Jobs are ephemeral containers;
reports and evidence stay local.

Copyright 2026 codepawfect. Licensed under the [Apache License 2.0](LICENSE).

## Quick start

Prerequisites: Python 3.10+, Git, Docker CLI with Compose v2, and a supported
container runtime. macOS uses Colima by default; Windows uses WSL2.

```bash
git clone https://github.com/CodePawfect/local-ci-harness.git
cd local-ci-harness

./ci init
./ci doctor
./ci lock-images --runtime

# Interactive setup TUI: detect repo, choose adapter, select stages.
./ci setup --repo /absolute/path/to/project
./ci prompt --repo /absolute/path/to/project

# Run only when the next action is a merge or push.
./ci gate --repo /absolute/path/to/project --intent merge-to-main
# After a local merge, while already on main:
./ci gate --repo /absolute/path/to/project --intent push-main
```

The setup writes `.ci/harness.json` and `.ci/agent-prompt.md` into the target
repository. The harness never commits, merges or pushes for you.

## Adapters and supported stacks

| Adapter | Works well for | Notes |
|---|---|---|
| `generic` | Any Git repository | Snapshot, Gitleaks, Semgrep and detectable dependency/IaC checks. Tests/build/coverage must be explicit. |
| `next-npm` | Next.js, React and TypeScript with npm | Install, lint, typecheck, tests, coverage, build, optional Playwright and Sonar. |
| `next-fullstack` | Next.js with server/API/integration code | Same as `next-npm`, plus explicit integration commands and test services. |
| `spring-maven` | Spring Boot, Java and a single Maven module | Surefire/Failsafe, JaCoCo, CycloneDX, Trivy and Sonar. |
| `custom` | Vite/React, Vue, Angular, Svelte, Python, Go, Rust and other stacks | Safe argv arrays and explicit evidence paths; no shell-string execution or guessed success criteria. |

Plain React/Vite is supported through `custom`; it does not yet have a dedicated
adapter. pnpm, Yarn, Gradle and multi-module Maven require explicit custom handling
or separate adapter work. Missing evidence is `BLOCKED`, never silently skipped.

## Monorepos

One profile evaluates one application directory inside the Git root:

```bash
./ci setup \
  --repo ~/repos/product \
  --subdir apps/web

./ci gate \
  --repo ~/repos/product \
  --intent merge-to-main
```

`apps/web`, `apps/api` and `services/catalog` are valid examples. Commands and
evidence paths are relative to the selected subdirectory. External paths and external
symlinks are rejected. V1 does not automatically aggregate multiple apps into one
coverage or Sonar result; use separate worktrees/profiles or an explicit aggregate
command.

## SonarQube

Sonar is optional. If the profile contains the `sonar` stage, provision it once:

```bash
./ci up
./ci bootstrap --repo /absolute/path/to/project
```

The bootstrap rotates the internal admin password, creates a least-privilege UI
account, assigns the project quality profile/gate and creates scoped analysis tokens.
To view results in the browser:

```bash
./ci sonar credentials
# Open http://127.0.0.1:9000 with the displayed read-only credentials.
./ci down                 # stop containers; keep persistent volumes
```

The admin password and UI password are local `0600` secrets under `.local/`; they are
never written to project profiles or reports. A normal profile gate starts and removes
the Sonar/Postgres containers automatically. Allocate more than 4 GB to Colima/Docker
for reliable JavaScript/TypeScript analysis.

## Agent prompt

Copy this into an agent working in the target repository. Replace the two paths:

```text
You are working in an existing local Git repository.

Use the Local CI Harness to verify this project before an explicitly requested merge
or push. Set:

HARNESS_DIR="/absolute/path/to/local-ci-harness"
PROJECT_DIR="/absolute/path/to/project"

1. Run `cd "$HARNESS_DIR" && ./ci doctor`.
2. If `"$PROJECT_DIR/.ci/harness.json"` does not exist, run
   `./ci setup --repo "$PROJECT_DIR" --non-interactive`. Do not use --force without
   user approval. Read the generated profile and agent prompt.
3. If the profile contains `sonar`, run `./ci up` and
   `./ci bootstrap --repo "$PROJECT_DIR"` once.
4. Run a gate only for an explicit intent:
   - feature branch ready to merge: `./ci gate --repo "$PROJECT_DIR" --intent merge-to-main`
   - already on main before push: `./ci gate --repo "$PROJECT_DIR" --intent push-main`
5. Read only the current run's `summary.json` and `summary.md`. Fix reproducible code
   failures and rerun the gate.

Never weaken tests, coverage thresholds, scanner rules, exclusions, timeouts or
evidence requirements. Stop on secrets, infrastructure failures or unclear security
findings. Stop after three failed repairs for the same cause. Never commit, merge,
push or write to main automatically.
```

## Results and exit codes

Reports are namespaced per project and run:

```text
reports/<project-slug>/<run-id>/summary.json
reports/<project-slug>/<run-id>/summary.md
.local/projects/<project-slug>/runs/<run-id>/
```

| Result | Exit code | Meaning |
|---|---:|---|
| `READY` | 0 | Required stages passed without warnings. |
| `REVIEW` / `FAIL` / `BLOCKED` | 1 | Review, verification failure or missing evidence. |
| `ERROR` | 2 | Configuration or infrastructure failure. |

## Platforms

- **macOS:** Docker CLI + Compose v2 and Colima. Colima is the default runtime.
- **Linux:** Docker Engine + Compose v2.
- **Windows:** run the harness inside WSL2 (Ubuntu or another Linux distribution),
  using Docker Desktop WSL integration or Docker Engine in WSL2. Native PowerShell
  and `cmd.exe` are not supported.

## Security model

Commands are stored as argument arrays, not shell strings. Central scanner policies,
image digests, gates and thresholds cannot be overridden by project profiles. Job
containers do not receive the host Docker socket. Builds and package installation
execute project code, so only run trusted repositories.

`.env`, `.local/` and `reports/` are local state and ignored by Git. Do not commit
credentials or generated reports.

## Documentation and contributing

- [Technical architecture](technical_doc.md)
- [Validation status and known limits](docs/VALIDATION.md)
- [Security test contract](docs/security-test-contract.md)
- [Research and source decisions](research.md)
- [Codex/agent template](templates/AGENTS.project.md)

Run the full test suite before a change:

```bash
python3 -m unittest discover -s tests -v
```

The current harness test suite contains 89 tests. Contributions that weaken a gate,
coverage threshold or security policy require explicit documentation and review.
