# Feature Specification: External Group Wiring — Agent ↔ Orchestrator Connection

**Feature Branch**: `003-external-group-wiring`
**Created**: 2026-04-16
**Status**: Draft
**Input**: User description: "External group creation is broken: when the bot is added to a brand-new group outside the usual workspaces, it sends one reply asking 'what is this group for' but then stays silent, and the orchestrator in HQ never sees the new agent. Fix the wiring so the group agent is a real agent (not a canned system reply) and so the orchestrator and the group can actually talk to each other."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Pre-announced external group gets a working agent (Priority: P1)

Roberto tells the orchestrator in HQ that he is about to create a new external group with a specific purpose (e.g., "I'm creating a group with Alice and Bob for the Nebula project — you'll be the workspace agent there, inherit skills from the astro-research workspace"). Roberto then creates the group on the messenger platform and adds the Robyx AI bot. The bot recognises that this group corresponds to the pre-announced intent, binds the prepared agent to the group with the instructions and skills the orchestrator already captured, and starts replying in the group as that agent. The orchestrator in HQ sees the new group appear in its registry and can route messages to it.

**Why this priority**: This is the golden path. It is the flow Roberto actually uses and the one that currently produces the broken behaviour. Without it, external collaboration with Robyx AI is unusable.

**Independent Test**: In HQ, announce to the orchestrator "create a group called X for purpose Y, inherit from workspace Z". Create the group, add the bot. Verify the bot's first message in the group references purpose Y (not a generic "what's this for?" template), and that subsequent messages in the group receive AI-generated replies consistent with that purpose. From HQ, ask the orchestrator "what external groups do we have?" and verify group X appears.

**Acceptance Scenarios**:

1. **Given** Roberto has told the orchestrator he will create an external group with a specific purpose and optional skill inheritance, **When** Roberto creates the group and adds the bot, **Then** the bot's first message in the group reflects the pre-announced purpose, and the agent is registered with the orchestrator as a reachable external group.
2. **Given** the bot has been added and the agent is wired up, **When** a group member sends a follow-up message, **Then** the agent produces an AI-generated reply grounded in the pre-announced purpose and any inherited skills.
3. **Given** the external group exists and is registered, **When** Roberto asks the orchestrator "send an update to the Nebula group" or similar, **Then** the orchestrator can locate the group and deliver the message to it.
4. **Given** the external group exists, **When** a decision taken in the group changes context the orchestrator should know about, **Then** the group agent can surface that change back to the orchestrator through a supported channel.

---

### User Story 2 — Ad-hoc group (no prior announcement) still produces a real agent (Priority: P2)

Roberto (or another authorised workspace member) adds the bot to a group without having told the orchestrator in advance. The bot detects that there is no matching pre-announcement. Instead of sending a hardcoded template and going silent, it starts a real AI-driven setup conversation in the group: it asks what the group is for and what skills should apply, it uses the creator's identity and any available workspace context to make informed suggestions, and every follow-up reply in the group is handled by a real agent. When setup answers are captured, the agent's instructions are updated accordingly and the orchestrator is notified that a new external group has come online with a summary of its purpose.

**Why this priority**: This is a reasonable fallback and matches a real usage pattern (Roberto forgets to announce in HQ first). Without it, the bot is stuck in a dead-end template.

**Independent Test**: Without any prior announcement in HQ, add the bot to a fresh group. Verify (a) the opening message is a real agent turn (can be varied across runs, not a byte-for-byte template), (b) follow-up messages receive AI-generated replies, (c) after the group answers the setup question, the agent's stored purpose/instructions reflect the answer, and (d) the orchestrator in HQ receives a notification that includes the captured purpose.

**Acceptance Scenarios**:

1. **Given** no pre-announcement exists for the creator, **When** the bot is added to a new group, **Then** the opening message is produced by the AI agent (not a canned template) and is consistent across the rest of the conversation.
2. **Given** the setup conversation is in progress, **When** a group member answers the setup question, **Then** the answer is captured and the agent's persistent instructions are updated to reflect the stated purpose and any inheritance.
3. **Given** setup has concluded, **When** Roberto checks the orchestrator in HQ, **Then** the new external group appears in the orchestrator's registry with its captured purpose.

---

### User Story 3 — Orchestrator routing to/from external groups (Priority: P2)

Once an external group is wired up, the orchestrator in HQ treats it as a first-class addressable destination. Roberto can direct the orchestrator to send messages, ask questions, or share context with a specific external group. Conversely, the group agent can post structured updates back to the orchestrator (e.g., "setup complete: purpose = X, members = A, B, C"; "the group decided Y"). The orchestrator's view of "what agents/groups exist" always matches reality.

**Why this priority**: Roberto's explicit complaint is that "the orchestrator does not see the new agent". Fixing the one-shot connection is necessary; establishing an ongoing two-way link is what actually unblocks collaboration over time.

**Independent Test**: With at least one external group live, from HQ ask the orchestrator to list external groups and to send a test message to one. Verify the message arrives in the correct group. From the group, trigger an event that should surface to HQ (e.g., "tell HQ we're done"). Verify the orchestrator receives it.

**Acceptance Scenarios**:

1. **Given** one or more external groups are wired up, **When** the orchestrator is asked to list them, **Then** all live external groups are returned with their purpose and creator.
2. **Given** an external group is live, **When** the orchestrator is instructed to send it a message, **Then** the message is delivered and attributed correctly in the group.
3. **Given** the group agent needs to surface information to HQ, **When** it invokes the upstream channel, **Then** the orchestrator receives the information and can act on it.

---

### Edge Cases

- **Bot added to a group it was already in** (re-add, or membership toggles): the existing external-group record must be reused; no duplicate agent is created.
- **Multiple pending pre-announcements by the same creator**: the most recent matching pre-announcement is used; older pending records are left untouched or expired, not silently consumed.
- **Bot added by a user who is not an authorised workspace member**: the bot should not freely provision an agent; it should either refuse setup, request authorisation from HQ, or leave the group, per the platform's security posture.
- **Setup question is never answered** (group goes silent for N days): the provisional agent should be marked stale so HQ can see it, and setup can be resumed or aborted without manual data cleanup.
- **Bot is removed from the group** (kicked, group deleted, migrated to a supergroup): the external-group record must transition to a closed/archived state so the orchestrator's registry does not drift out of sync with reality.
- **The pre-announced skills reference a workspace that no longer exists** (renamed/deleted): setup must degrade gracefully (ask the user to pick another source or start fresh) instead of crashing or silently dropping the skill inheritance.
- **User creates the group on a platform where external groups are not yet supported** (Discord, Slack): the current behaviour (Telegram-only) must at minimum fail loudly and inform Roberto, rather than appearing to work and then going silent.
- **Race between pre-announcement and group creation**: if the group is created before the pre-announcement is fully persisted, the bot must not assume the group is ad-hoc; a short grace window or explicit retry is preferable to permanently misclassifying the group.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST allow an authorised workspace member to pre-announce an intent to create an external group, capturing at minimum the intended purpose and optional skill inheritance, and persisting that intent until either (a) the group is created and bound, or (b) the intent expires or is cancelled.
- **FR-002**: When the bot is added to a new external group, the system MUST attempt to match the event to a pending pre-announcement by the user who added the bot, and on a match MUST bind the prepared agent (with its captured purpose and skills) to the new group.
- **FR-003**: When the bot is added to a new external group and no pending pre-announcement matches, the system MUST start an AI-driven setup conversation in the group — the first reply to the group MUST be produced by a real agent turn, not a hardcoded template string.
- **FR-004**: Every subsequent message in the external group (during setup and afterwards) MUST be handled by a real agent that has access to the group's captured purpose, skills, and workspace context. Silent drops are not acceptable.
- **FR-005**: The orchestrator MUST maintain an authoritative registry of live external groups that is consistent with the underlying persisted state, and MUST be able to list them on request from an authorised user.
- **FR-006**: The orchestrator MUST be able to send messages or instructions to any live external group by name/identifier, with delivery confirmed or a clear error surfaced when delivery is not possible.
- **FR-007**: The external-group agent MUST be able to surface information back to the orchestrator through a supported upstream channel (e.g., the HQ control room), so decisions made in the group can propagate.
- **FR-008**: When the AI-driven setup conversation captures the group's purpose and/or inheritance, the system MUST update the agent's persistent instructions to reflect those answers, so the same context is honoured on future turns and after restart.
- **FR-009**: The system MUST notify the orchestrator in HQ whenever a new external group is wired up (both pre-announced and ad-hoc paths), including the group's captured purpose, creator, and identifier, so the orchestrator's view stays in sync.
- **FR-010**: External-group records MUST transition to an appropriate terminal state (archived/closed) when the bot is removed from the group, the group is deleted, or the group migrates, and the orchestrator's registry MUST reflect that transition.
- **FR-011**: The system MUST enforce that only authorised workspace members can cause an external-group agent to be provisioned; the behaviour when an unauthorised user adds the bot MUST be explicit (refuse / escalate to HQ / leave), not silent provisioning.
- **FR-012**: The system MUST log enough information about external-group lifecycle events (added, bound, setup complete, archived, errors) for Roberto to diagnose a broken flow without reading source code.
- **FR-013**: For this iteration the wiring is scoped to Telegram (the only platform currently handling the "bot added to group" event). On Discord and Slack, the system MUST surface an explicit "external groups not yet supported on <platform>" message to the user who triggered it (or to HQ), rather than appearing to succeed and then going silent. Extending to Discord and Slack is a follow-up, tracked separately.

### Key Entities

- **External Group**: A chat on a messenger platform, external to the usual workspaces, where Roberto and collaborators work with a dedicated Robyx AI agent. Attributes include: stable identifier, display name, creator, purpose, skill inheritance source (if any), lifecycle state (pending → setup → active → archived), associated agent, and platform of origin.
- **Pre-announcement (Pending External Group)**: An intent captured before the group physically exists, owned by a specific creator, carrying the purpose and any inheritance the orchestrator should hand off when the bot is added. Expires or is consumed on bind.
- **External-Group Agent**: The AI agent bound to a specific external group, with instructions derived either from a pre-announcement or from the AI-driven setup conversation, and reachable both from inside the group and from the orchestrator in HQ.
- **Orchestrator Registry Entry**: The orchestrator's view of a single external group — the minimum information needed for the orchestrator to list, address, and reason about the group.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In 100% of pre-announced external-group creations, the bot's first in-group message references the pre-announced purpose (verifiable by inspecting the message content against the captured intent).
- **SC-002**: In 100% of external-group creations (pre-announced or ad-hoc), every user message sent in the group within the first 10 minutes receives an agent reply — no silent drops.
- **SC-003**: After an external group is wired up, the orchestrator in HQ can list and address it on the first try, with zero manual state reconciliation steps required from Roberto.
- **SC-004**: Roberto's reported failure mode ("I create the group, get one message, then silence, and HQ doesn't see the agent") is fully reproducible today and fully absent after this work, confirmed by an end-to-end test covering both pre-announced and ad-hoc paths.
- **SC-005**: When the bot is removed from a live external group, the orchestrator's registry reflects the archived state within one minute, with no lingering "phantom" groups.
- **SC-006**: Setup-conversation answers persist across a process restart — killing and relaunching the bot after setup completes does not lose the captured purpose or inherited skills.

## Assumptions

- The existing persistence layer for collaborative workspaces (`CollabStore` / `collaborative_workspaces.json`) is the right foundation to extend; no parallel store needs to be introduced.
- The orchestrator already has a mechanism for addressing agents by name/thread; this feature extends that mechanism to external groups rather than replacing it.
- Pre-announcements are captured through the existing HQ conversation flow; no separate UI/command is required.
- Authorisation for provisioning an external-group agent is tied to existing workspace membership/roles; no new role system is introduced.
- The initial target platform is Telegram, which is the only platform currently wiring the "bot added to group" event. Extending to Discord and Slack may be scoped separately — see FR-013.
- "Real agent turn" means invocation of the same AI pipeline used elsewhere in the bot, with the same skill/context loading — not a separate lightweight responder.
- The orchestrator's view of external groups must be derived from (or kept in sync with) the authoritative persisted state; it is not an independent cache that can drift.
