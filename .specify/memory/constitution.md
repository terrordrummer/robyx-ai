<!--
  Sync Impact Report
  ===================
  Version change: 0.0.0 → 1.0.0 (initial ratification)
  
  Added principles:
    - I. Multi-Platform Parity
    - II. Chat-First Configuration
    - III. Resilience & State Persistence
    - IV. Comprehensive Testing
    - V. Safe Evolution
  
  Added sections:
    - Technology Stack & Constraints
    - Development Workflow
    - Governance
  
  Templates requiring updates:
    ✅ plan-template.md — Constitution Check section is generic; no update needed
    ✅ spec-template.md — No constitution-specific references; compatible
    ✅ tasks-template.md — No constitution-specific references; compatible
    ✅ No command templates found in .specify/templates/commands/
  
  Follow-up TODOs: none
-->

# Robyx Constitution

## Core Principles

### I. Multi-Platform Parity

Every user-facing feature MUST work identically across all supported
messaging platforms (Telegram, Discord, Slack). Platform adapters
MUST implement the full `Platform` ABC defined in
`bot/messaging/base.py`. A feature merged without coverage on all
three adapters MUST be documented as a known limitation in the
release notes and tracked for follow-up.

Rationale: Users can switch platforms at any time; workspaces,
agents, and memory are preserved across migrations. Asymmetric
behaviour breaks this promise.

### II. Chat-First Configuration

All runtime configuration — workspaces, agents, specialists,
scheduled tasks, model preferences, and `.env` overrides — MUST be
achievable through the messaging interface without editing files or
dashboards. Config files (`.env`, `models.yaml`) exist as
persistence, not as the primary interface.

Rationale: Robyx's value proposition is "Clone. Configure. Talk."
If the user must leave the chat to configure something, that
contract is violated.

### III. Resilience & State Persistence

The system MUST survive unclean restarts without data loss. All
mutable state — agent registrations, scheduler queue, continuous
task progress, memory — MUST be persisted to the `data/` directory
atomically. On recovery the scheduler MUST late-fire any events
that were missed while the process was down. The single-instance
lock (`bot.pid`) MUST prevent concurrent execution.

Rationale: Robyx runs as a system service with keep-alive. Crashes,
OOM kills, and host reboots are expected operational events, not
exceptional ones.

### IV. Comprehensive Testing

The test suite MUST exercise every public contract: platform
adapters, scheduler paths (interactive, one-shot, periodic,
continuous), migration chains, handler routing, and AI backend
abstraction. New features MUST include tests that cover the golden
path and at least one error/edge case. The test suite MUST pass
before any release tag is cut.

Rationale: With 960+ tests already in place, regressions are the
primary risk vector. Untested code is unshippable code.

### V. Safe Evolution

Releases MUST follow the tag-based auto-update flow: pre-update
snapshot, smoke test, atomic rollback on failure. Database or state
schema changes MUST use the migration framework
(`bot/migrations/vX_Y_Z.py`) with a monotonic version chain.
Migrations MUST be idempotent — re-running an already-applied
migration MUST be a no-op. Breaking changes to the `data/`
directory contract MUST be documented in the release notes and
the migration MUST handle the transition automatically.

Rationale: Users run Robyx unattended as a service. A bad update
that corrupts state or breaks the scheduler without automatic
recovery is a service outage with no human in the loop.

## Technology Stack & Constraints

- **Language**: Python 3.10+
- **Platforms**: Telegram (Bot API), Discord (discord.py),
  Slack (Socket Mode via slack-sdk)
- **AI Backends**: Claude Code, Codex CLI, OpenCode — selected
  per-agent via semantic aliases or explicit model IDs
- **Storage**: JSON files under `data/` (no external database)
- **Testing**: pytest; all tests in `tests/`
- **Service management**: launchd (macOS), systemd (Linux),
  Task Scheduler (Windows)
- **License**: MIT

Constraints:
- No external database dependencies; the `data/` directory is
  the single source of truth at runtime.
- Voice transcription requires an OpenAI API key (optional).
- The bot MUST operate correctly with a single AI backend
  installed; multi-backend is additive, not required.

## Development Workflow

- **Branching**: Feature branches off `main`; Linear-generated
  branch names preferred (`username/ROB-{n}-{slug}`).
- **Commits**: Reference Linear issue IDs where applicable
  (`Closes ROB-{n}`).
- **Releases**: Semantic versioning via `VERSION` file.
  Each release gets a `bot/migrations/vX_Y_Z.py` file if state
  changes are needed, a `releases/vX.Y.Z.md` notes file, and
  a `CHANGELOG.md` entry.
- **Code review**: All changes affecting scheduler, handlers,
  or platform adapters MUST be reviewed against multi-platform
  parity (Principle I) before merge.
- **Migration discipline**: New migrations MUST be generated
  via `scripts/new_migration.py` to ensure chain continuity.

## Governance

This constitution is the authoritative reference for architectural
and process decisions in Robyx. When a proposed change conflicts
with a principle above, the principle takes precedence unless the
constitution is amended first.

**Amendment procedure**:
1. Propose the change with rationale.
2. Update this document with the new or revised principle.
3. Increment the version per semantic versioning:
   - MAJOR: principle removal or incompatible redefinition.
   - MINOR: new principle or material expansion.
   - PATCH: clarification, wording, or typo fix.
4. Update `LAST_AMENDED_DATE`.
5. Propagate changes to dependent templates if affected.

**Compliance**: All PRs and reviews SHOULD verify alignment with
these principles. Complexity beyond what the task requires MUST
be justified in the PR description.

**Version**: 1.0.0 | **Ratified**: 2026-04-16 | **Last Amended**: 2026-04-16
