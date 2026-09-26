# Reviewed false-positive findings

## Owner authorization — 26 September 2026

For PhysioPose PR #6 the owner approved the two exact exceptions below provided
that they are false positives. The owner also explicitly permits agents to create
and document future exceptions for conclusively verified false-positive findings.
See the bounded standing policy in `AGENTS.md`. Uncertain findings and changes to
coverage, test assertions, scan rules, severity or gates still require owner review.

## PhysioPose historical public configuration

Verified by Astra and an independent Luna audit against the original Git blobs.
Gate `20260926T111112-4b3f1a98` scanned 272 commits at application HEAD
`4697d94c170a39b2d786a35431dee3ee55feaeb8`; its current-tree scan was clean.
No credential values are reproduced here.

| Exact finding fingerprint | Evidence and classification |
| --- | --- |
| `d86c93019d2c27b6c4a2db8d72e1eaa0fd01a9b1:docs/social-auth.md:generic-api-key:26` | Public `passkey_enabled=true` and WebAuthn relying-party domain configuration. No authentication secret or key material. |
| `6d776cf00cada9e46e1c09c02e7ae10af2917368:docs/superpowers/plans/2026-04-24-templates-feature.md:generic-api-key:143` | `VITE_SUPABASE_PUBLISHABLE_KEY` with verified `sb_publishable_` prefix: intentionally public browser configuration, not a service-role or secret key. |

Only these immutable commit/path/rule/line fingerprints are present in
`policy/gitleaks.ignore`. No file-wide, rule-wide or token-pattern exception is
introduced. Future occurrences and every other current/historical finding remain
subject to the existing detector rules. Since these exact Git blobs are immutable,
there is no time-based expiry; remove an exception if its classification is shown
to be wrong. Do not rotate intentionally public configuration as if it were a secret.

Validation completed: 100 harness unit tests passed. Fresh full PhysioPose gate
`20260926T112356-ef10d97c` passed at `4697d94c170a39b2d786a35431dee3ee55feaeb8`,
including the unchanged current-tree and 272-commit history scans. All 519 project
tests passed; changed-code coverage was 80.9% against the existing 80% minimum.
Sonar analysis `83335f80-9257-47a8-a213-b7ab46f41dfe` returned quality gate OK.
The prior failed run remains failed; the exceptions alone are not gate approval.
