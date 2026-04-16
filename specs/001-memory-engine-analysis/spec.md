# Feature Specification: Memory Engine Evolution

**Feature Branch**: `001-memory-engine-analysis`
**Created**: 2026-04-16
**Status**: Draft
**Input**: User description: "Analyze alternative memory engines to replace markdown-based memory, with tiered memory architecture for long-lived agent conversations"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Workspace agent maintains project state across months of conversations (Priority: P1)

A user works with a workspace agent on a complex project over several months. The project evolves through multiple phases: initial setup, feature development, debugging, refactoring. The agent's understanding of the project MUST remain accurate and current regardless of how many conversations have occurred. When the user starts a new conversation, the agent immediately knows the project's current state, active decisions, open TODOs, and known issues without the user re-explaining anything.

**Why this priority**: This is the core value proposition of agent memory — without reliable project-state recall, every conversation starts from scratch, destroying the "AI staff" experience.

**Independent Test**: Create a workspace agent, conduct 50+ interactions spanning project changes, verify the agent's context accurately reflects the latest state at conversation start.

**Acceptance Scenarios**:

1. **Given** a workspace agent with 6 months of history, **When** a new conversation begins, **Then** the agent's context contains an accurate, up-to-date overview of the project (status, decisions, TODOs, gotchas) without loading the full conversation history.
2. **Given** a workspace agent where multiple decisions have been superseded, **When** the agent references a past decision, **Then** only the current decision is in active context; superseded decisions are accessible only on demand.
3. **Given** active memory approaching the word budget (5000 words), **When** new information is added, **Then** obsolete entries are archived automatically and active memory stays within budget.

---

### User Story 2 - Historical context retrieval on demand (Priority: P2)

A user asks the agent "what did we decide about X three months ago?" or "show me the history of changes to the auth module." The agent retrieves relevant historical entries from the archive without those entries polluting the active context. The retrieval is fast and relevant even across hundreds of archived entries.

**Why this priority**: Historical recall is critical for accountability and understanding past decisions, but it's accessed infrequently compared to current-state memory.

**Independent Test**: Archive 200+ entries across multiple quarters, query for specific topics, verify retrieval relevance and speed.

**Acceptance Scenarios**:

1. **Given** an archive with 200+ entries spanning 4 quarters, **When** the user asks about a specific past decision, **Then** the system retrieves the relevant entries within 2 seconds.
2. **Given** an archive with entries about multiple topics, **When** the user asks about topic X, **Then** only entries relevant to topic X are returned, not the entire archive.
3. **Given** no matching entries in the archive, **When** the user asks about a topic never discussed, **Then** the system reports no relevant history found rather than returning false matches.

---

### User Story 3 - Memory survives context compaction without information loss (Priority: P2)

A user has a very long conversation (spanning multiple context compactions by the AI backend). Key information discussed early in the conversation — decisions, TODOs, code references — remains available after compaction. The agent's behavior is consistent before and after compaction events.

**Why this priority**: Long conversations are the primary use case for workspace agents working on complex tasks. If memory is lost at compaction boundaries, users cannot trust the agent for sustained work.

**Independent Test**: Conduct a conversation that triggers 3+ compaction events, verify that key facts from the beginning are still available after the last compaction.

**Acceptance Scenarios**:

1. **Given** a conversation where 3 compaction events have occurred, **When** the user references a decision from the first segment, **Then** the agent still knows the decision and its rationale.
2. **Given** the AI backend triggers compaction, **When** the conversation resumes, **Then** the consolidated project state is injected into the new context alongside any conversation-specific state.

---

### User Story 4 - Multi-agent memory isolation and cross-referencing (Priority: P3)

Multiple workspace agents operate independently on different projects. Each agent's memory is isolated — Agent A cannot accidentally see Agent B's context. However, when Robyx (the orchestrator) needs a cross-project view, it can aggregate state from all workspace agents.

**Why this priority**: Memory isolation prevents context pollution between projects, while cross-referencing enables orchestration-level oversight.

**Independent Test**: Create 3 workspace agents, verify memory isolation, verify Robyx can aggregate their states.

**Acceptance Scenarios**:

1. **Given** 3 workspace agents with separate projects, **When** Agent A's memory is loaded, **Then** it contains zero information from Agent B or Agent C.
2. **Given** Robyx requests a cross-project overview, **When** the orchestrator aggregates workspace states, **Then** a summary of each workspace's current status is available without loading full agent memories.

---

### Edge Cases

- What happens when the memory store corrupts (incomplete write, disk full)?
- How does the system handle concurrent writes from scheduled tasks and interactive sessions on the same agent?
- What happens when an agent's project directory is moved or deleted?
- How does memory behave during auto-updates and migrations?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST support a tiered memory architecture with at least two tiers: active (loaded into context) and archive (queryable on demand).
- **FR-002**: Active memory MUST provide a current-state overview of the workspace/project that is accurate as of the last interaction.
- **FR-003**: Archive memory MUST support topic-based retrieval, returning only entries relevant to a query rather than entire archive files.
- **FR-004**: The memory engine MUST handle at least 10,000 archived entries per agent without degradation in query performance.
- **FR-005**: All memory operations (read, write, archive) MUST be atomic — partial writes MUST NOT corrupt existing memory state.
- **FR-006**: The memory system MUST remain compatible with all three AI backends (Claude Code, Codex, OpenCode) — memory is injected as text into prompts regardless of backend.
- **FR-007**: The system MUST detect and respect projects with native Claude Code memory (.claude/ directory), falling back to Robyx memory only when native memory is absent.
- **FR-008**: Memory content MUST survive bot restarts, auto-updates, and migration chains without data loss.
- **FR-009**: The system MUST support per-agent memory isolation — no agent can access another agent's memory unless explicitly authorized (orchestrator aggregation).
- **FR-010**: The memory engine MUST operate without requiring external services — all dependencies MUST be embeddable or bundled (no separate database server).

### Key Entities

- **MemoryTier**: Represents a storage tier (active, archive) with its own retention policy and access pattern.
- **MemoryEntry**: A discrete unit of memory — a decision, TODO, state snapshot, or observation — with metadata (timestamp, source agent, topic tags).
- **MemoryStore**: The persistence backend for a specific agent's memory (could be files, SQLite, or hybrid).
- **MemoryQuery**: A retrieval request with optional filters (time range, topic, keyword).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An agent with 12 months of history loads its current-state context in under 500ms.
- **SC-002**: Topic-based retrieval from an archive of 10,000 entries returns relevant results in under 2 seconds.
- **SC-003**: Active memory stays within its word budget (currently 5000 words) automatically, without manual intervention by the user.
- **SC-004**: Zero data loss across 100 simulated crash-and-recovery cycles.
- **SC-005**: Memory storage for a typical workspace agent (1 year of daily use) stays under 50MB.
- **SC-006**: The migration from the current markdown-based system to the new engine is fully automatic — users do not need to take any manual action.

## Assumptions

- Mid-term conversational memory (tracking what was discussed within the current topic session) is delegated to the AI backend's native compaction mechanism (e.g., Claude's `/compact`). Robyx does NOT manage intra-session context — it provides the memory that gets injected at session start.
- The current two-tier architecture (active + archive) is conceptually sound; the question is whether the storage engine (markdown files) scales, not whether the tier model needs redesign.
- Semantic/vector search for archive retrieval is a desirable upgrade but NOT a hard requirement — keyword-based retrieval is an acceptable baseline.
- No external services (PostgreSQL, Redis, Elasticsearch) will be introduced — the solution MUST be self-contained within the Python runtime (SQLite, embedded vector store, or file-based).
- Performance targets are based on a single-user deployment (one Robyx instance per user), not multi-tenant.
