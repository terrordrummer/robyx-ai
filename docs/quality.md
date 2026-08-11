# Quality gates and incremental architecture

Robyx runs the required quality gates on every currently supported Python minor:
3.10, 3.11, 3.12, 3.13, and 3.14. The policy is a ratchet: critical defects and
regressions fail immediately, while measured legacy debt is reduced through
bounded changes instead of hidden by repository-wide exemptions.

## Local gate

Use a dedicated virtual environment and the development lock matching the
interpreter. For Python 3.14, for example:

```bash
python -m pip install uv==0.11.7
uv pip sync requirements/locks/dev-py314.txt
ruff check bot tests setup.py
mypy
pytest -ra --cov=bot --cov-report=term-missing \
  --cov-report=json:coverage.json --cov-fail-under=80
python scripts/check_coverage.py coverage.json
python scripts/check_constraints.py --check
```

Use `dev-py310.txt` through `dev-py314.txt` for the corresponding interpreter.
`uv pip sync` removes packages absent from the selected lock, so never run it
against a shared environment.

## Supported-version matrix

GitHub Actions executes lint, typing, the full test suite, warning policy,
overall coverage, and risk-path coverage on all five supported minors. A new
Python minor is not supported merely because an installer can find its binary:
its runtime and development locks and green CI job must land first. Dropping a
minor is a separate compatibility decision, not a quality-gate shortcut.

## Lint ratchet

Ruff currently blocks `E9`, `F63`, `F7`, and `F82`: syntax failures, invalid
constructs, and unresolved names that commonly become import-time or runtime
failures. The broader integrated `E`/`F` review baseline contains 134 findings:
81 unused imports, 42 imports below module code, 10 unused variables, and one
legacy import redefinition. Inspect it with:

```bash
ruff check bot tests setup.py --select E4,E7,E9,F
```

Do not add repository-wide ignores for that debt. Each decomposition should
clean and enable the next safe rule family for its owned modules. The next
ratchets are `F841`, then `F401`, then `E402`; generated or compatibility code
may receive a narrow, documented per-file exception only when restructuring it
would change behaviour.

## Warning policy

Pytest displays every warning and promotes runtime and deprecation warnings to
errors. Two known diagnostics have precise `default` filters in `pytest.ini`,
so they remain visible in the warning summary rather than being suppressed:
discord.py's Python 3.12 `audioop` deprecation and the narrowly classified
CPython `AsyncMockMixin._execute_mock_call` unawaited-coroutine diagnostic.
Resource and pytest-unraisable warnings also remain visible while existing
cleanup debt is repaired. The logging-handler leak exposed by the policy was
fixed by closing the test-owned handler. Remove an exception when its upstream
or test fix lands; do not broaden its category, message, or module pattern.

## Gradual typing boundary

Bare `mypy` checks the new execution/task-ownership authority, persistence
recovery, runtime supervision, maintenance transaction, local permission,
dependency-lock, and config-command service modules listed in
`pyproject.toml`. Imports outside that boundary are skipped until their owners
are typed; inside it, incomplete signatures, implicit optionals, invalid
returns, stale ignores, and equality mistakes fail CI. Expansion order is:

1. `config_schema.py`, after its generic `typed_env` return contract is explicit;
2. the next extracted scheduler/updater service object;
3. adapter protocols and the remaining orchestration entrypoints.

## Reproducible dependencies

Human-edited dependency inputs remain `bot/requirements.txt`,
`tests/requirements-test.txt`, and `requirements/quality.in`. Generated locks
are split by purpose and Python minor:

- `runtime-py310.txt` through `runtime-py314.txt` contain only production
  dependencies. macOS, Linux, and Windows installers, startup bootstrap, and
  updater select the exact current-minor lock and install it with
  `--require-hashes`.
- `dev-py310.txt` through `dev-py314.txt` add test and quality tools and are used
  by CI and contributors.

The universal locks contain hashes and platform markers so Windows, macOS, and
Linux do not inherit a maintainer's local `pip freeze`. Resolution is pinned to
uv 0.11.7 and excludes releases newer than 2026-08-11. Regenerate and verify all
ten files with:

```bash
python scripts/check_constraints.py --write
python scripts/check_constraints.py --check
```

Do not hand-edit generated locks. For declared minors, a missing lock,
unsupported selection, pip launch failure, timeout, or non-zero install exits
with an actionable error: there is no silent fallback to unlocked or stale
dependencies. Bootstrap's dependency marker fingerprints both the human input
and selected runtime lock; updater failures enter the existing rollback path.

## Coverage risk paths

The repository gate is 80% overall. Per-file floors prevent operational
boundaries from regressing. Legacy floors are never below the measured review
baseline; newly introduced security and resilience boundaries start at 85% or
higher.

| Boundary | Review baseline | Enforced floor | RR-11 closure | Next target |
|---|---:|---:|---:|---:|
| `bot.py` startup/platform runners | 28% | 28% | 47.0% | 60% |
| `scheduler.py` claim/dispatch/reconcile | 68% | 68% | 74.4% | 80% |
| `orphan_tracker.py` crash cleanup | 46% | 46% | 83.6% | 90% |
| Telegram adapter lifecycle/media | 53% | 53% | 56.0% | 70% |
| execution authority policy | 100% | 100% | 100% | 100% |
| persistence recovery | 84% | 85% | 85.2% | 90% |
| runtime supervision | 73% | 85% | 85.6% | 90% |
| dedicated-topic recovery | new | 85% | 90.0% | 95% |
| local permission hardening | 70% | 85% | 86.2% | 90% |
| typed configuration schema | 81% | 85% | 97.9% | 90% |
| dependency lock selection | new | 85% | 97.7% | 95% |
| config command service | new | 85% | 100% | 95% |
| maintenance reader/writer gate | new | 85% | 96.3% | 98% |
| canonical task ownership scope | new | 85% | 95.9% | 98% |

The measurement column is the Python 3.12 full-suite RR-11 closure; the
repository total is 82.09%. Enforced values are the gate, not claims that
testing is complete. The next-target column records deliberate future work.
Raise a floor in the same change that adds focused tests; never lower it to make
a build pass.

## Complexity map and decomposition sequence

At a complexity threshold of 15, 27 functions remain over budget. The initial
incremental extraction is complete: direct configuration parsing, validation,
preflight, response ordering, and restart handling moved from
`handlers.make_handlers` into the typed, focused-tested
`ConfigCommandService`. The public handler callables and response text stayed
stable. Concurrent destination and collaborative-authority hardening added
branches in the same factory, so its raw score is now 342 and is not a useful
standalone measure of this extraction's value.

### `handlers.make_handlers` (C901 342)

Continue one responsibility at a time while retaining the factory as the
compatibility composition root:

1. `CollaborationCommandService` for claim, role resolution, announce, and
   participant refusal paths using `ChatRef` and `InvocationSecurityContext`;
2. `WorkspaceLifecycleService` for task/event/archive macros;
3. a small `HandlerRegistry` wiring platform callbacks to those services.

Each extraction first locks the existing closure contract with focused tests,
moves only one responsibility, and preserves message ordering and error text.

### `scheduler.run_scheduler_cycle` and continuous dispatch

Separate pure queue decisions from I/O:

1. a typed `ClaimBatch`/`DispatchDecision` layer for due-entry selection;
2. `ContinuousDispatcher` owning generation checks, child registration, and
   lifecycle-lock interaction;
3. `ResultReconciler` owning atomic state/queue outcomes;
4. keep `run_scheduler_cycle` as orchestration over those collaborators.

Keep integration tests around atomic claims and real child cancellation. The
claim lock and its write remain one atomic boundary.

### `updater.apply_update` (C901 59)

Model the existing transaction without weakening rollback:

1. `ReleaseResolver` returns the verified tag, object, commit, and version;
2. `UpdateSnapshot` owns verified snapshot creation and staged restore;
3. `CodeInstaller` owns exact-commit install, locked dependencies, and smoke test;
4. `UpdateTransaction` records phases and compensates in reverse order.

Retain fault-injection tests for every rename/install/restore boundary. Child
process supervision is tracked separately and is not part of this extraction.
Historical migrations remain immutable upgrade evidence unless a reproducible
migration defect is found.
