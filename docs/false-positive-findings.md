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

## PhysioPose analytics LocalStorage name — 30 September 2026

Gate `20260930T202041-7d9df307` at application commit
`74fdd58c1a01410252160630f1b798ae08a389d4` reported the literal
`physiopose-ga4-consent` as `generic-api-key`. The source uses it solely to
read a browser LocalStorage consent choice; it is not an authentication token,
credential, or access grant. The historical blob was inspected with `git show`
and contains the identical public storage name.

Exact immutable history fingerprints added to `policy/gitleaks.ignore`:

- `74fdd58c1a01410252160630f1b798ae08a389d4:src/ga4Runtime.ts:generic-api-key:3`
- `c1acb3df1b8a59f726f11b2f2c778910ddf9f356:src/ga4.ts:generic-api-key:4`

For directory scans, `policy/gitleaks.toml` requires both the exact source path
`src/ga4Runtime.ts` and the complete captured literal `physiopose-ga4-consent`,
scoped to `generic-api-key`. A changed value at the same path remains detectable.
There is no whole-file or whole-rule exclusion. This uses the existing owner's
bounded authorization for conclusively verified false positives. The original
failed gate remains failed. Fresh scan/gate evidence will be recorded below.

Validation: 100 harness unit tests passed. The pinned scanner accepted the exact
public literal and blocked a changed synthetic credential-like value at the same
path. Full gate `20260930T203657-fd5057d0` at `4f47a4f81ce508108441dbf28f4801822aa70820`
ended READY: both fresh secret scans passed, 82.6% changed-code coverage met the
existing 80% minimum, and Sonar analysis `99c36a7d-2588-40ae-937f-50d90ff1b5c2`
returned quality gate OK with zero new security/reliability findings.
