# Additional Semgrep rules

Additional locally reviewed Semgrep rules can be versioned here as `*.yml` or
`*.yaml`. The runner loads these files in addition to `../semgrep.yml`; they are
included in the run's policy hash. Normal scans do not fetch mutable registry
rules. All policy files go through the same source and policy integrity checks.

The nine project rules in `../semgrep.yml` are a small supplement to Sonar's
Java/JS/TS profiles, not a complete OWASP Top 10 rule pack. Add a reviewed
OWASP/Java/React rule pack only with documented source, revision, license, and
positive/negative rule tests.

Review false positives and new blockers before updates. Proprietary or Pro rules
may require additional rights or a different engine; the runner explicitly uses
`--oss-only`.

`ERROR` blocks the run; `WARNING` is reported as `WARN` with findings in the JSON
report. Syntax/parser errors and zero analyzed files are failures, not passes.
