# Spec-Driven Development Status

_Maintained project snapshot. Last reconciled against code, tests, release
history, and task checklists on 2026-08-13._

- **Project**: robyx-ai
- **Branch**: `main`
- **Constitution**: defined (v1.0.0, ratified 2026-04-16)
- **Release version**: `0.29.3`
- **Remediation register**: [`REVIEW_REMEDIATION.md`](../REVIEW_REMEDIATION.md)

## Features

| Feature | Specify | Plan | Tasks | Evidence-based status |
|---|:---:|:---:|:---:|---|
| 001-memory-engine-analysis | ✓ | ✓ | ✓ | Complete (45/45) |
| 002-full-code-review | ✓ | ✓ | ✓ | 93/121; shipped security scope, 28 documented P2 items deferred |
| 003-external-group-wiring | ✓ | ✓ | ✓ | Complete (65/65) |
| 004-fix-continuous-task-macro | ✓ | ✓ | ✓ | Complete (67/67) |
| 005-unified-workspace-chat | ✓ | ✓ | ✓ | 71/72; shipped in v0.23.0, live-credential smoke T071 remains manual |
| 006-continuous-task-robustness | ✓ | ✓ | ✓ | 64/72; shipped in v0.26.0, eight literal P2 deltas documented in its checklist |
| 007-discord-parity | ✓ | ✓ | ✓ | 53/54; shipped in v0.28.0, real-guild quickstart T051 remains manual |

Legend: ✓ artifact exists and is reconciled. A partial task count is not treated
as an implementation failure when the checklist documents a safer equivalent or
an explicitly manual validation boundary.

## Verification state

- Release `0.29.0` captures the completed August remediation program;
  `v0.29.1` adds the launcher-portable Linux CI assertion without changing
  runtime behaviour, and `v0.29.2` restores a `0.0.0` compatibility floor so
  every updater-capable historical Robyx release can select it directly.
  `v0.29.3` retires legacy periodic system-monitor notifications while
  preserving on-demand diagnostics and all unrelated queue entries.
- Release gate: **2,513 passed, 1 skipped** locally on Python 3.12; the locked
  Python 3.10–3.14 GitHub matrix also passes on the release commit.
- Python 3.12 coverage: **82.14%** overall; every committed risk-path ratchet
  passes, including all new security/resilience boundaries at 85% or higher.
- Critical Ruff, gradual mypy, dependency-lock drift, compileall, POSIX installer
  syntax, and `git diff --check` gates pass.
- Release metadata is present through `v0.29.3`; annotated tags remain
  immutable and are created only after the corresponding commit passes CI.

## Open validation and P2 work

- Spec 002 retains its 28 explicitly deferred stability/UX/natural-interaction
  review items.
- Spec 005 T071 and spec 007 T051 require real platform credentials and remain
  operator smoke tests; automated runner/adapter coverage is green.
- Spec 006 leaves eight non-blocking wording/coverage deltas open: T007, T009,
  T011, T013, T027, T033, T052, and T060. Their implemented equivalents and
  exact remaining gaps are recorded beside the checklist.
- The repository-wide RR-00–RR-11 program has no unresolved P0/P1 finding. Its
  accepted operational residuals and recovery boundaries live in
  `REVIEW_REMEDIATION.md` rather than being hidden in completed task counts.

## Next action

Run the two credentialed smoke tests in their target environments when those
credentials are available. Future P2 work starts from the open lists above and
must preserve the quality ratchets.
