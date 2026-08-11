# Robyx Review Remediation Program

This is the completed source of truth for the repository-wide review started on
2026-08-11 and closed on 2026-08-12. It is deliberately self-contained so a new
agent context can understand the implementation, evidence, and accepted
residuals without relying on chat history.

The older [BACKLOG.md](BACKLOG.md) is a completed historical remediation plan.

## Objective

Resolve the correctness, security, resilience, and maintainability findings from
the August 2026 deep review while preserving existing behaviour and user data.
Work is incremental: one independently verifiable ticket at a time, with focused
tests before the full-suite gate.

## Resume Protocol

Every new context MUST follow this sequence:

1. Read `AGENTS.md` and this file completely.
2. Run `git status --short`; preserve unrelated or user-owned changes.
3. Select the first `in_progress` ticket, or the first dependency-ready `todo`
   ticket when none is active. If every ticket is `done`, do not reopen this
   program implicitly; start from the recorded residuals and create a new
   bounded ticket.
4. Read the ticket's evidence, contracts, affected modules, and latest work-log
   entry before editing.
5. Reproduce the issue or run its focused baseline tests.
6. Implement only the ticket scope; add regression tests and update relevant
   documentation.
7. Run the ticket verification commands and then the full suite when required by
   its completion gate.
8. Update the ticket status, evidence, decisions, residual risks, and Work Log in
   this file. Record meaningful shipped changes under `CHANGELOG.md` →
   `Unreleased`.

Do not mark a ticket `done` merely because code exists. Its completion gate must
be satisfied and recorded.

## Status Vocabulary

- `todo`: not started and dependency-ready only when all dependencies are done.
- `in_progress`: actively being implemented; at most three non-overlapping
  tickets may be active when coordinated by a primary agent.
- `blocked`: cannot progress safely; the blocking condition and required input
  must be written into the ticket and Work Log.
- `done`: implementation, tests, documentation, and recorded evidence complete.

Priorities: `P0` security/data-loss/runtime-contract risk, `P1` important
resilience and operational integrity, `P2` maintainability and delivery quality.

## Baseline Evidence

Captured on 2026-08-11 at commit `13d73f4`:

- Git worktree: clean.
- Tests: 2,086 passed, 1 skipped on Python 3.12 and on a clean Python 3.14 venv.
- Coverage: 80% overall; risk-path gaps include `bot.py` 28%, `scheduler.py`
  68%, `orphan_tracker.py` 46%, and Telegram adapter 53%.
- Ruff default rules: 139 findings, 85 mechanically fixable.
- Complexity threshold `C901 > 15`: 22 functions; `make_handlers` is the largest
  hotspot.
- No repository CI, type-check gate, dependency lock/constraints file, or
  supported-Python test matrix.

## Program Order

```text
RR-00
 ├─ RR-01 ─┐
 ├─ RR-02 ─┼─ RR-03 ─┐
 └─────────┘          │
 RR-04 ─ RR-05 ───────┼─ RR-11
 RR-06 ─ RR-07 ───────┤
 RR-08 ─ RR-09 ───────┤
 RR-10 ────────────────┘
```

`RR-01` and `RR-02` may run in parallel because their source/test ownership is
separate. `RR-03` must incorporate the final Discord routing contract. Remaining
P1 tickets should normally be completed before broad architectural work.

## Ticket Summary

| Ticket | Priority | Status | Goal | Depends on |
|---|---:|---|---|---|
| RR-00 | P0 | done | Establish durable plan, evidence, and handoff protocol | — |
| RR-01 | P0 | done | Repair Discord collaborative routing and reconnect lifecycle | RR-00 |
| RR-02 | P0 | done | Make continuous stop/complete/delete terminate and drain work | RR-00 |
| RR-03 | P0 | done | Enforce collaborative execution authority in code | RR-01, RR-02 |
| RR-04 | P1 | done | Recover or fail closed on corrupted queue/continuous state | RR-02 |
| RR-05 | P1 | done | Add typed config validation, preflight, and rollback | RR-04 |
| RR-06 | P1 | done | Make updates version-pinned, snapshot-safe, and reversible | RR-00 |
| RR-07 | P1 | done | Supervise asyncio tasks and child-process shutdown | RR-01, RR-02 |
| RR-08 | P1 | done | Bound Slack media downloads and align adapter retry policy | RR-00 |
| RR-09 | P1 | done | Harden local permissions and secret input paths | RR-00 |
| RR-10 | P2 | done | Add quality gates and reduce high-risk complexity incrementally | RR-01–RR-09 |
| RR-11 | P0 | done | Final cross-platform, lifecycle, security, and documentation audit | RR-01–RR-10 |

## Tickets

### RR-00 — Persistent remediation control plane

- Priority: `P0`
- Status: `done`
- Owner: primary coordinating agent
- Scope: this register, link from historical backlog, initial changelog entry,
  execution order, and context-resume protocol.
- Completion gate:
  - register is self-contained and tracked;
  - every review finding maps to a ticket;
  - active delegated work is reflected accurately;
  - repository documentation identifies this file as the active source of truth.
- Evidence: review baseline above.
- Resolution:
  - completed on 2026-08-11;
  - all review findings map to RR-01 through RR-11;
  - delegated P0 ownership and the cross-context resume protocol are recorded;
  - `BACKLOG.md` identifies this file as the active source of truth.
- Residual risk: none.

### RR-01 — Discord collaborative bridge and reconnect safety

- Priority: `P0`
- Status: `done`
- Owner: delegated agent `/root/discord_p0`, coordinated by primary
- Evidence:
  - `bot/bot.py::on_message` rejects all non-owner messages before collaborative
    role routing;
  - the runner supplies a raw guild id while Discord workspace identity is the
    canonical `<guild>:<channel>` `ChatRef`;
  - `on_ready` creates boot/scheduler/update tasks on every reconnect.
- Required outcome:
  - owner, operator, and participant messages reach the correct collaborative
    workspace and retain their distinct roles;
  - HQ remains owner-only;
  - voice and command messages use the same unambiguous chat identity;
  - reconnects do not duplicate boot effects or background loops.
- Completion gate:
  - regression tests cover owner/operator/participant, non-collaborative
    rejection, and repeated ready events;
  - focused adapter/handler/runner tests pass;
  - full suite passes;
  - Discord/team documentation matches runtime behaviour.
- Resolution:
  - the runner no longer rejects non-owner messages before role resolution and
    emits canonical `<guild>:<channel>` identity while preserving the native
    channel id for delivery;
  - handler lookup, manual claim, authorization, clear, setup, advisory, and
    refusal paths share the same `ChatRef` conversion;
  - voice transcripts re-enter `handle_message`, so collaborative roles derive
    the identical typed execution profile used by text and non-collaborative
    voice remains global-owner-only;
  - retained boot/scheduler/updater tasks are idempotent across repeated ready
    events and restart only after termination;
  - Discord runner plus collaboration regression coverage passed independently
    (167 tests) and in the combined 2,163-test suite; README/team documentation
    now reflects Discord parity.
- Residual risk: no live Discord gateway was contacted; the runner boundary is
  exercised with realistic adapter events and mocks.

### RR-02 — Atomic continuous-task lifecycle

- Priority: `P0`
- Status: `done`
- Owner: delegated agent `/root/continuous_p0`, coordinated by primary
- Contract: `specs/006-continuous-task-robustness/contracts/lifecycle-ops.md`
- Evidence:
  - stop, complete, and delete mutate state/cancel queue entries but do not
    terminate the active subprocess;
  - a detached step can rewrite `state.json` after a terminal user action;
  - dispatch and state transitions are not protected by a shared per-task
    generation/ownership check.
- Required outcome:
  - lifecycle operations terminate and drain active subprocesses within the
    configured bound;
  - a stale step cannot resurrect stopped/completed/deleted state;
  - operations are idempotent and preserve the contract's topic/history rules;
  - workspace close and explicit lifecycle operations share one implementation.
- Completion gate:
  - tests exercise a real or realistic live subprocess during stop, complete,
    and delete;
  - state remains terminal after late step completion;
  - focused lifecycle/scheduler tests and full suite pass;
  - spec task status is reconciled with actual implementation.
- Resolution:
  - stop/pause/complete/delete cancel the queue before yielding, signal the live
    process immediately, bound grace to five seconds, reload post-exit state,
    and only then write the authoritative lifecycle transition;
  - a per-task lock serializes concurrent lifecycle mutations with generation
    checks, while the scheduler reaps a child when a lifecycle operation wins
    during spawn;
  - resume reactivates canceled work, delete remains idempotent until a new
    generation reuses the name, and recreation purges only the matching canceled
    continuous tombstone;
  - 112 focused tests and the combined 2,163-test suite pass. Spec-006 tasks
    T043 and T045–T049 are reconciled as complete; T044/T050/T051 remain open
    rather than being claimed by this ticket.
- Residual risk: closed by RR-07/RR-11 process-group ownership, durable process
  identity, descendant termination, and watcher shutdown.

### RR-03 — Enforceable collaborative authorization

- Priority: `P0`
- Status: `done`
- Owner: delegated agent `/root/collab_security_design`, coordinated by primary
- Depends on: `RR-01`, `RR-02`
- Evidence: Claude, Codex, and OpenCode run with permissive host access by
  default, while participant non-executive status is communicated only through
  prompt text.
- Required outcome:
  - participant turns cannot invoke write/shell capabilities even when their
    prompt attempts to override instructions;
  - executive authority is decided by application code and propagated as a
    typed execution policy;
  - safe defaults remain usable across all three backends;
  - existing owner/operator workflows have an explicit migration path.
- Completion gate:
  - adversarial authorization tests prove participant tool denial;
  - backend command tests prove the selected sandbox/permission profile;
  - configuration and security documentation clearly state the boundary;
  - full suite passes.
- Decision: implement D-002 incrementally. Participant turns are stateless and
  fail closed under a backend-native read-only profile; executive/system lanes
  retain their current behaviour. Read-only is an integrity boundary, not a
  confidentiality boundary.
- Resolution:
  - `InvocationSecurityContext` is mandatory at the AI boundary and the sole
    Role-to-profile mapping lives in authorization code; participant turns do
    not reuse or mutate executive sessions and cannot interrupt a privileged
    lane or dispatch side-effect markers;
  - Claude receives only Read/Glob/Grep with plan/safe/no-persistence and empty
    MCP; Codex uses an isolated read-only profile with 61 explicit unsafe
    feature disables and rejects unknown enabled features; OpenCode resolves a
    child-only deny-by-default config with read/glob/grep allowlisted;
  - offline runtime probes accept the installed Claude 2.1.227, Codex 0.147.0,
    and OpenCode 1.18.15 CLIs, while missing or changed capabilities fail closed;
  - 283 focused, 518 expanded, and 2,163 full-suite tests pass (1 skipped).
- Residual risks:
  - read-only protects integrity, not confidentiality of readable workspace
    content;
  - upstream CLI semantics are probed, not cryptographically attested;
  - upstream backend behavior is capability-probed rather than
    cryptographically attested; all OpenCode lanes now use child-local config.

### RR-04 — Corruption recovery for queue and continuous state

- Priority: `P1`
- Status: `done`
- Owner: delegated agent `/root/state_recovery_rr04`, coordinated by primary
- Depends on: `RR-02`
- Evidence: malformed `queue.json` is converted to `[]`; a later mutation can
  overwrite the only copy and lose all queued work. Corrupted continuous state
  similarly becomes unavailable without quarantine/recovery.
- Required outcome:
  - distinguish missing, valid, and corrupt data;
  - quarantine corrupt originals and recover a verified snapshot when possible;
  - fail closed on mutation when recovery is impossible;
  - expose an actionable operator event without leaking data.
- Completion gate: corruption-before-add/update/delete tests, crash-interrupted
  recovery tests, documentation, and full suite pass.
- Resolution:
  - a shared recovery layer distinguishes missing, valid, and corrupt JSON,
    preserves unique forensic copies, and uses a durable recovery marker;
  - updater snapshots are scanned newest-to-oldest and only an exact, regular,
    bounded member with the expected JSON shape is installed atomically;
  - queue and continuous-state mutators fail closed without a valid recovery;
    scheduler cycles report `degraded` plus `queue_unavailable` and journal the
    incident instead of overwriting state;
  - 161 focused tests and the combined 2,163-test suite pass; scheduler and data
    directory documentation describe recovery and reconciliation.
- Residual risk: a verified snapshot can be stale; the CRITICAL event/log is an
  explicit operator reconciliation boundary.

### RR-05 — Typed and reversible configuration updates

- Priority: `P1`
- Status: `done`
- Owner: delegated agent `/root/typed_config_rr05`, coordinated by primary
- Depends on: `RR-04`
- Evidence: direct chat updates validate known names but not values; invalid
  integer/enum input is written and the immediate restart can crash at import.
- Required outcome: one typed schema for setup/chat/runtime; range and enum
  validation; atomic write; fresh-process preflight; rollback on failure;
  actionable reply without echoing secrets.
- Completion gate: invalid-value and simulated restart tests cover every
  chat-editable key; valid existing configurations remain compatible.
- Resolution:
  - a stdlib-only schema defines all 11 chat-editable fields and is reused by
    setup, runtime imports, and direct-chat parsing for enum, bounded integer,
    executable, directory, string, and boolean validation;
  - explicit invalid or mixed assignments are handled locally before the AI
    boundary; participant policy and identity/platform credentials remain
    non-chat-editable;
  - updates use an atomic private file replacement followed by a fresh isolated
    Python import of the candidate; any failure restores the previous bytes and
    mode before replying, and child output/secret values are redacted;
  - 129 focused, 551 integration, and 2,203 full-suite tests pass (1 skipped).
- Residual risks: config locking is process-local; POSIX mode enforcement is
  best-effort under Windows stdlib semantics; preflight imports all runtime
  configuration but does not start transports. RR-09 closed the local secret
  and permission surfaces, and RR-11 gates config changes during maintenance.

### RR-06 — Transactional, version-pinned updater

- Priority: `P1`
- Status: `done`
- Owner: delegated agent `/root/updater_rr06`, coordinated by primary
- Evidence: `apply_update(version)` pulls current `main` and records the requested
  version without proving that `HEAD` is that tag; snapshot failure is non-fatal;
  rollback is not anchored to the captured pre-update SHA.
- Required outcome: trusted exact-tag fetch/verification, mandatory verified
  data snapshot when relevant, commit/version consistency check, rollback to the
  captured SHA, and explicit handling of rollback failure.
- Completion gate: integration tests with divergent main/tag, snapshot failure,
  migration failure, rollback failure, and stash conflict all pass.
- Resolution:
  - the updater fetches the advertised remote tag into an updater-owned ref,
    verifies its object/peeled commit plus release metadata and `VERSION`, and
    installs that exact commit even when `origin/main` differs;
  - non-empty runtime data requires an atomically written, fully read and
    manifest-validated snapshot; pruning cannot remove the active transaction;
  - rollback targets the captured pre-update SHA and verifies both HEAD and
    VERSION. Data restore uses verified sibling staging and compensating
    same-filesystem directory renames, preserving the only recoverable copy on
    injected failures;
  - 113 updater tests, a real divergent-main/tag Git integration test, and the
    combined 2,163-test suite pass.
- Residual risk: `origin` remains the trust root without signed-tag enforcement.
  RR-11 added writer quiescence, durable transaction recovery, supervised
  migration/dependency children, and verified in-memory reload after restore.

### RR-07 — Background task and subprocess supervision

- Priority: `P1`
- Status: `done`
- Owner: delegated agent `/root/supervision_rr07`, coordinated by primary
- Depends on: `RR-01`, `RR-02`
- Required outcome: central retained task registry or supervisor; exception
  reporting; idempotent startup; bounded cancellation/drain at shutdown; no
  ignored scheduled-delivery watchers.
- Completion gate: reconnect, task-exception, SIGTERM/shutdown, and orphan-child
  tests pass on supported platforms.
- Resolution:
  - a process-wide supervisor retains named/idempotent maintenance tasks,
    delivery watchers, participant probes, interactive AI children, and
    scheduled/continuous children, with done-callback exception reporting;
  - shutdown first rejects new spawn, signals isolated process groups, escalates
    after a bounded grace, reaps and preserves evidence for any stubborn child,
    then cancels and retrieves background tasks; Telegram, Slack, Discord,
    signal, and atexit paths all invoke the appropriate async or bounded sync
    hook;
  - scheduler watchers are retained even without a delivery platform and own
    lock/PID cleanup. Participant probes propagate cancellation without caching
    a false capability result;
  - scheduler OpenCode configuration is now child-specific, eliminating the
    remaining process-global permissive `OPENCODE_CONFIG` mutation;
  - 312 initial focused tests, 34 independent coordinator tests, a real child
    process test, and the final 2,207-test suite pass (1 skipped).
- Residual risk: synchronous signal/atexit fallback can only make a bounded
  best-effort reap because the event loop may already be unavailable; any PID
  that cannot be reaped remains in the durable orphan registry for next boot.

### RR-08 — Slack media and adapter reliability

- Priority: `P1`
- Status: `done`
- Evidence: Slack voice download buffers the complete response and has no 25 MB
  cumulative cap.
- Required outcome: streamed download with content-length and cumulative limit,
  bounded redirects, partial-file cleanup, and a documented/reused adapter retry
  policy for reply/edit operations.
- Completion gate: oversized, missing-length, redirect, interrupted-stream, and
  cleanup tests pass.
- Resolution:
  - Slack voice files stream in 64 KiB chunks with a 25 MB header and cumulative
    limit, at most five redirects, and allowlist validation at every hop;
  - temporary files are removed on oversize, HTTP/stream/file errors, and task
    cancellation; replies and edits reuse the platform retry policy;
  - 52 adapter tests, 269 adjacent integration tests, an independent combined
    72-test cross-workstream gate, and the 2,163-test full suite pass;
  - media limits and retry behaviour are documented.
- Residual risk: file writes are synchronous per bounded 64 KiB chunk; this is
  acceptable at the current 25 MB cap but can move to an async file API if
  profiling shows event-loop latency.

### RR-09 — Local permissions and secret hygiene

- Priority: `P1`
- Status: `done`
- Owner: delegated agent `/root/permissions_rr09`, coordinated by primary
- Evidence: setup writes `.env` and creates `data/` without enforcing private
  modes; non-interactive token flags can be visible in history/process lists.
- Required outcome: `.env` and runtime files `0600`, private directories `0700`,
  migration for existing installations, and non-argv secret input paths.
- Completion gate: Unix permission tests, Windows-compatible no-op behaviour,
  installer documentation, and upgrade-path test pass.
- Resolution:
  - POSIX boot and service installation establish umask `0077`, recursively
    repair `.env`, logs, and `data/` to file `0600`/directory `0700`, refuse
    symlinks/non-regular runtime paths, and fail before config import if the
    private invariant cannot be established;
  - workspace-local `.robyx` SQLite databases plus WAL/SHM are created or
    repaired privately at connection open, covering memory outside `data/`;
  - interactive setup uses no-echo input; non-interactive setup supports
    current-user-owned private files and `ROBYX_SETUP_*` environment sources
    for all five secrets. Legacy value-bearing argv flags warn without values;
  - credential, identity, participant-policy, and sandbox assignments in chat
    are intercepted before collaborative/AI routing and neither applied nor
    echoed;
  - 491 focused/integration tests, 222 independent coordinator tests, and the
    2,241-test full suite pass (1 skipped); local security coverage is 86% and
    memory-store coverage 92%.
- Residual risks: Windows stdlib cannot express equivalent ACL hardening;
  workspace memories from existing installs are repaired on first open rather
  than enumerated globally; installations intentionally using symlinks inside
  private runtime paths now fail closed and require a real private directory.

### RR-10 — Quality gates and incremental decomposition

- Priority: `P2`
- Status: `done`
- Owner: delegated agent `/root/quality_rr10`, coordinated by primary
- Depends on: `RR-01` through `RR-09`
- Required outcome:
  - CI matrix for supported Python versions;
  - configured Ruff and warnings policy;
  - gradual type-checking boundary;
  - reproducible dependency constraints/lock strategy;
  - split high-risk responsibilities from `make_handlers`, scheduler, and updater
    without a broad rewrite.
- Completion gate: required checks run locally and in CI; risk-path coverage
  targets are recorded and met; architecture documentation reflects ownership.
- Resolution:
  - CI and local gates cover every declared Python 3.10–3.14 minor with separate
    universal, hash-verified runtime and development locks; bootstrap, updater,
    and all installers resolve the exact running-minor lock and fail closed;
  - critical Ruff rules, an explicit warning policy, gradual mypy boundaries,
    and risk-path coverage ratchets are versioned in `pyproject.toml`,
    `pytest.ini`, and `scripts/`; all new boundaries met their 85% floor at the
    RR-10 checkpoint;
  - configuration command parsing/application moved from `make_handlers` into
    typed `ConfigCommandService`, with ordering and behavioral equivalence tests;
  - installers stop and bound-wait the managed service before recreating the
    live virtual environment;
  - 271 focused tests plus 53 lock/bootstrap/config documentation tests pass;
    Ruff, mypy, constraint drift, all ten lock installs on Python 3.10–3.14,
    shell syntax, compileall, and diff-check pass.
- Residual risk: Windows PowerShell behavior is contract-tested statically but
  was not executed on a live Windows host; the final Python 3.10–3.14 matrix and
  expanded coverage gate are recorded in RR-11.

### RR-11 — Final audit and program closure

- Priority: `P0`
- Status: `done`
- Depends on: all previous tickets
- Required outcome: repeat the full review, reconcile specs 006/007, eliminate
  README/platform contradictions, archive or migrate historical task lists, and
  document any consciously accepted residual risk.
- Completion gate:
  - full tests, coverage, lint, type, and clean-install checks pass;
  - no unresolved P0/P1 finding remains;
  - `git diff --check` and clean generated-artifact check pass;
  - this program is marked complete with final evidence and release notes.
- Closure ledger (2026-08-12):
  - **closed** — foreign Telegram chats can no longer be mistaken for HQ,
    including owner commands, voice, collab-registry failure, and colliding
    thread ids; every ordinary destination passes one central guard;
  - **closed** — collaborative agents cannot dispatch continuous/lifecycle,
    plan, archive, event, restart, focus, or HQ control markers; only the
    explicitly collaborative image/reminder/setup/notify surface remains;
  - **closed** — participant AI is fail-closed `disabled` by default. The
    explicit `read-only` opt-in retains its documented integrity-only boundary;
  - **closed** — Telegram forwards otherwise-unregistered slash commands to the
    shared command router; lifecycle commands and Discord `/clear` use the same
    handler/macro engine, and unknown HQ commands never become AI prompts;
  - **closed** — successful assistant prose containing transport-error words is
    delivered once; retries inspect failed-process diagnostics only;
  - **closed** — AgentManager, collaborative, migration, queue/continuous, and
    orphan registries validate, recover, or fail closed without empty reseeds;
    migration-tracker corruption aborts all platform boot sequences;
  - **closed** — process-tree lifecycle kill, PID-reuse protection, close drain,
    topic recovery/HQ fallback, runtime UI transitions, and same-name creation
    use durable identities, bounded cleanup, and compensation;
  - **closed** — an exclusive maintenance transaction quiesces writers,
    checkpoints SQLite, supervises updater/bootstrap/config children, and
    completes cancellation-safe rollback before normal work resumes;
  - **closed** — canonical `(platform, chat, parent thread)` ownership reaches
    state and queue, while revisioned `program.json` authority prevents stale
    step writes from losing an accepted plan update;
  - **closed** — spec-006 orphan recovery, delete-mid-drain delivery,
    `drain_timeout` parsing, adapter retry/WARN parity, offline quickstart, HQ
    source audit, and 10,000-event performance gates are present;
  - **closed** — prompts, session invalidation, platform portability claims,
    data-recovery guidance, command docs, task checklists, spec status, and
    Unreleased notes match the runtime.
- Final evidence:
  - **2,505 passed, 1 skipped** independently on each locked Python 3.10, 3.11,
    3.12, 3.13, and 3.14 environment;
  - Python 3.12 coverage **82.09%** overall; every risk-path floor passes,
    including persistence recovery 85.22%, runtime supervision 85.57%, topic
    recovery 89.96%, maintenance 96.35%, and task scope 95.88%;
  - critical Ruff, mypy on eight typed boundaries, all ten dependency-lock
    drift checks, compileall, POSIX installer syntax, and `git diff --check`
    pass;
  - spec 005 is reconciled at 71/72, spec 006 at 64/72 with eight explicit P2
    deltas, and spec 007 at 53/54; unverified credentialed smoke tests remain
    open instead of being reported as automated evidence.
- Accepted residual risks:
  - participant `read-only` is an explicit integrity-only opt-in, not a
    confidentiality sandbox; keep the default `disabled` for sensitive data;
  - POSIX signaling retains a small fingerprint-check-to-signal TOCTOU window
    without `pidfd`, and Windows `taskkill /T` is weaker than Job Objects;
  - unsigned remote tags leave `origin` as the update trust root;
  - an externally uncertain HQ send is claimed before delivery to avoid
    duplicates, so a rare ambiguous failure may suppress one alert;
  - two live-platform quickstarts (spec 005 T071 and spec 007 T051), eight
    literal spec-006 P2 deltas, and visible test/upstream warning hygiene remain
    future work. No unresolved P0/P1 finding remains.

## Decision Log

### D-001 — Durable roadmap plus coordinated implementation

- Date: 2026-08-11
- Decision: use this tracked register as the handoff boundary while a primary
  agent coordinates bounded implementation tasks. Do not rely on chat history as
  project state.
- Reason: the program spans multiple contexts and tightly coupled runtime paths;
  durable evidence prevents duplicated work while coordination allows parallel
  progress on non-overlapping P0 scopes.
- Consequence: every implementation turn must update this register before
  handoff.

### D-002 — Code-enforced participant execution profile

- Date: 2026-08-11
- Decision: introduce a typed invocation security context derived from the
  persisted `Role`, propagate it through handlers and `invoke_ai` to backend
  command construction, and implement a fail-closed participant profile.
- Participant contract:
  - no write/edit/shell mutation, network, MCP, browser, or subagent capability;
  - stateless invocation with no reuse or mutation of the executive session;
  - Claude restricted to its read-only tool set, Codex `read-only` sandbox with
    isolated configuration/environment, OpenCode deny-by-default child config;
  - unsupported/obsolete backend capability rejects the participant turn rather
    than falling back to full access.
- Executive/system contract: current autonomy remains unchanged in this ticket.
- Configuration: local-only `COLLAB_PARTICIPANT_POLICY=disabled|read-only`, with
  fail-closed `disabled` as default; never expose a chat-settable `full` mode.
- Reason: prompt tags and response-marker stripping happen too late to prevent
  subprocess effects. Backend policy must derive from application authorization,
  not from model interpretation.
- Limitation: the explicit read-only opt-in protects workspace integrity but may
  expose readable workspace content. Keep participants disabled for sensitive
  workspaces; a conversation-only isolated worker remains a future option for
  fully untrusted collaborators.
- Required implementation note: add the collaborative prompt template to session
  invalidation and reset affected sessions through `AgentManager.reset_sessions`.

### D-003 — Canonical task scope and revisioned program authority

- Date: 2026-08-12
- Decision: every new scheduler/reminder/continuous entry carries a typed
  `(platform, chat_id, parent_thread_id)` scope. Public enqueue APIs fail closed
  without it; legacy state is migrated only when ownership is unambiguous.
- Decision: continuous program fields live in revisioned `program.json` and are
  overlaid on step state at read/write boundaries, so a stale child cannot
  overwrite an accepted `[UPDATE_PLAN]` transaction.
- Reason: thread ids are not globally unique, and atomic file replacement alone
  does not prevent read-modify-write lost updates.
- Consequence: migration-only code must opt into legacy unscoped queue writes;
  ordinary runtime callers cannot silently create ambiguous ownership.

### D-004 — Maintenance is a fail-stop transaction

- Date: 2026-08-12
- Decision: updater work owns an exclusive maintenance lease, durably marks the
  transaction before mutation, drains runtime writers/processes, supervises all
  children, and completes shielded rollback/reload before releasing the gate.
- Reason: a snapshot of live SQLite/WAL data or cancellation after checkout can
  otherwise produce a tree that is neither the old release nor the new one.
- Consequence: if recovery cannot be verified within its bound, the marker and
  poisoned gate remain; startup/work fail closed until an operator completes
  recovery. Availability is never preferred over unverified state.

## Work Log

### 2026-08-11 — Review baseline and remediation kickoff

- Completed repository map, full tests on Python 3.12 and 3.14, coverage, Ruff,
  complexity, security, concurrency, lifecycle, updater, and documentation audit.
- Created RR-00 through RR-11 and the context Resume Protocol.
- Started RR-01 with delegated scope `/root/discord_p0`.
- Started RR-02 with delegated scope `/root/continuous_p0`.
- Requested a read-only enforceable authorization design for RR-03 from
  `/root/collab_security_design`.
- Next checkpoint: review delegated diffs independently, run focused tests, merge
  decisions into this register, then run the complete suite before closing the
  first P0 increment.

### 2026-08-11 — First implementation checkpoint

- RR-01 implementation complete pending combined full-suite/documentation gate:
  canonical Discord channel identity, role-aware bridge, and reconnect-safe boot
  loops; independent focused verification: 167 tests passed.
- RR-02 implementation complete pending combined full-suite/spec gate: bounded
  termination, authoritative post-drain state, spawn-race guard, per-task
  lifecycle serialization, resumable queue reactivation, and delete→recreate
  generation handling; delegated full-suite checkpoint: 2,101 passed, 1 skipped
  before concurrent updater edits.
- D-002 accepted and RR-03 implementation started with participant stateless,
  backend-native read-only, fail-closed semantics.
- RR-06 updater transaction work and RR-04 corruption recovery started on
  non-overlapping scopes.

### 2026-08-11 — P0 closure and P1 checkpoint

- Closed RR-01 through RR-04 after independent review and the combined gate:
  Discord role routing/reconnect safety, atomic continuous lifecycle, typed
  participant authorization, and fail-closed state recovery are implemented and
  documented.
- Closed RR-06 after exact-tag Git integration and updater fault injection; code
  and runtime snapshots retain a recoverable pre-transaction copy on failure.
- Closed RR-08 after an independent Slack/recovery/Discord cross-workstream gate
  (72 passed) and the final full suite (2,163 passed, 1 skipped).
- Reconciled the implemented spec-006 lifecycle tasks without marking the still
  missing dedicated golden-message and slash-command tasks complete.
- Started RR-05 (`/root/typed_config_rr05`) and RR-07
  (`/root/supervision_rr07`) on explicitly separated file ownership. Next
  checkpoint: review both diffs, run combined gates, then start RR-09.
- Closed RR-05 after its divided full-suite gate (2,203 passed, 1 skipped) and
  started RR-09 (`/root/permissions_rr09`) only after setup/config ownership was
  released. Review also identified that direct local-only secret assignments
  must be intercepted rather than routed to the AI; that regression is included
  explicitly in RR-09.
- Closed RR-07 after a coordinator-requested follow-up brought participant CLI
  probes under process supervision and retrieved stubborn shutdown waiter tasks;
  final gate: 2,207 passed, 1 skipped. OpenCode no longer needs a global
  configuration export on any execution lane.
- Closed RR-09 after two review follow-ups extended hardening to workspace-local
  SQLite memory and made permission errors fail before `.env` import. Its final
  integrated gate passed 2,241 tests (1 skipped); RR-10 may now modify the
  previously protected setup/install boundaries.
- RR-10 phase A added warning/lint/type/coverage gates and reproducible CI locks,
  then review withheld closure because Python 3.10/3.11/3.13 are documented as
  supported and production installers were not yet consuming the locks. The
  follow-up must cover every declared minor, split runtime/dev locks, integrate
  bootstrap/updater/installers, enforce honest coverage ratchets, and extract
  only the configuration command responsibility from the handler hotspot.
- Pre-final audit found a missed cross-surface authorization bug: collaborative
  voice was still wrapped by global `owner_only` and its transcript entered the
  executive text router. The coordinator fixed it before RR-11; participant
  voice now proves `PARTICIPANT_READ_ONLY` in regression tests (14 focused
  voice/role tests passed).

### 2026-08-12 — RR-11 closure

- Repeated the audit across routing, participant authority, persistence,
  scheduler/lifecycle races, subprocess trees, updater cancellation, installer
  ordering, prompts, commands, platform portability, and release/spec records.
- Closed the remaining cross-chat task ownership and lost-plan-update risks with
  D-003; closed update/writer/cancellation hazards with D-004.
- Added fail-closed semantic recovery for every critical JSON registry,
  dedicated-topic recreation/HQ fallback, process identity and descendant
  cleanup, delete-mid-drain journaling, orphan recovery, and explicit
  continuous timeout parsing.
- Reconciled spec 005 (71/72), spec 006 (64/72), and spec 007 (53/54) from code
  evidence. Manual or literal P2 gaps remain open and described; nothing was
  blanket-marked complete.
- The first five-version run exposed bootstrap mutating an unrelated local
  `.venv` under Python 3.13/3.14. Bootstrap now runs only when `sys.prefix`
  owns the managed checkout environment; 48 focused dependency/install tests
  and the repeated matrix pass.
- Final verification: 2,505 passed and 1 skipped on each of Python 3.10–3.14;
  82.09% overall coverage with all risk ratchets green; Ruff, mypy, lock drift,
  compileall, installer shell syntax, and diff-check green.
- Packaged the completed program as the non-breaking `0.29.0` release with a
  continuous no-op data migration, verified release metadata, and annotated
  tag `v0.29.0` on the release commit.
- RR-00 through RR-11 are complete. Start any future P2 cleanup as a new bounded
  program rather than silently reopening this historical closure record.
