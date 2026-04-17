# Quickstart: External Group Wiring

**Feature**: 003-external-group-wiring
**Audience**: Roberto and anyone reviewing the feature end-to-end.

This quickstart exercises the two user-facing flows (P1 pre-announced, P2 ad-hoc) and the orchestrator's view of external groups (P2). It assumes a running bot on Telegram with HQ configured (`OWNER_ID`, `CHAT_ID`, and `control_room_id` set).

---

## Prerequisites

- Robyx bot running as a service (`launchctl list | grep robyx` or equivalent).
- You are the owner (`OWNER_ID`) and have access to HQ.
- You have a second Telegram account (or a willing collaborator) to add to the external group, so the bot isn't alone with you in it.
- Logs are tailing, filtered to `collab.*`:

  ```bash
  tail -F <log-path> | grep 'collab\.'
  ```

## Flow 1 — Pre-announced external group (P1)

1. **In HQ, talk to the orchestrator**:

   > "I'm going to create a Telegram group with Alice and Bob for the Nebula research project. Use our astro-research workspace as a base."

2. **Expected HQ response** — a natural-language confirmation followed by (behind the scenes) a `[COLLAB_ANNOUNCE ok: name=nebula]` trailer. Logs show `collab.announce name=nebula creator_id=<OWNER_ID> purpose="…"`.

3. **Create the group on Telegram**:
   - New group → name it "Nebula Research" → add your test collaborator → add the Robyx bot.

4. **Expected in the new group within ~2s**:

   > "Nebula Research — collaborative workspace is ready. I'm the agent for this workspace. …"

   The first message **references the pre-announced purpose** (SC-001). Logs show `collab.match ws_id=<…> chat_id=<…>`.

5. **Expected in HQ**:

   > "*Collaborative workspace configured* — Workspace *nebula* is now linked to group _Nebula Research_ …"

6. **Talk in the group**. Every message you send (as owner) or Alice/Bob send (as participants) receives an AI-generated reply from the Nebula agent, grounded in the astro-research skills. No silent drops (SC-002).

7. **Ask HQ**: "what external groups do we have?" — the orchestrator lists Nebula with its purpose.

8. **Send a message to the group via HQ**: "Tell Nebula we'll skip tomorrow." — delivered via `[COLLAB_SEND name="nebula" …]`; appears in the group attributed to the bot; HQ shows `[COLLAB_SEND ok: nebula]`.

---

## Flow 2 — Ad-hoc group, no pre-announcement (P2)

1. **Do NOT tell HQ anything**. Directly on Telegram, create a new group, add your test collaborator, add the Robyx bot.

2. **Expected in the group within ~5s** (first message is an AI turn, not a template):

   > "Hey — thanks for adding me. What should this workspace focus on? If you'd like me to inherit from an existing workspace (astro-research, dev-playground, …), tell me which. Otherwise we'll start fresh."

   Note: the exact wording varies run-to-run. That **IS the acceptance test** (SC-004): two fresh runs produce different wording.

3. **Reply in the group** (from the adder's account):

   > "Let's work on photon-calibration experiments. Start fresh."

4. **Expected in the group**: an AI reply that acknowledges the purpose and confirms setup is done. Behind the scenes the agent emits `[COLLAB_SETUP_COMPLETE purpose="…" inherit="" inherit_memory="true"]` which the handler strips.

5. **Expected in HQ**:

   > "*Collaborative workspace setup complete* — Workspace *collab-photon-calibration* (`collab-photon-calibration`) is now active. Purpose: photon-calibration experiments. Inherits: none (memory: true). chat_id: -100…"

6. **Verify persistence across restart** (SC-006):

   ```bash
   # Restart the service
   launchctl kickstart -k user/$(id -u)/com.robyx.bot
   ```

   After restart, send a message in the group. It receives an AI reply grounded in "photon-calibration experiments" — the purpose survived.

---

## Flow 3 — Lifecycle: remove the bot

1. **In a live external group, kick the bot** (via Telegram group settings).

2. **Expected logs**: `collab.archive ws_id=<…> reason=bot_removed`.

3. **Expected in HQ**: "Collaborative workspace *<name>* has been closed (bot removed from group)."

4. **Ask the orchestrator**: "what external groups do we have?" — the closed group is no longer listed (SC-005).

---

## Flow 4 — Unsupported platform (FR-013, Discord/Slack)

If the bot is added to a Discord guild or a Slack channel:

1. **Expected single message in the new scope**:

   > "External collaborative groups are not yet supported on Discord. Use Telegram for external groups. The bot will take no further action here."

2. **Expected logs**: `collab.unsupported_platform platform=discord chat=<id>`.

3. No `CollabWorkspace` is created; no follow-ups are answered.

---

## Flow 5 — Unauthorised adder (FR-011)

1. Have a **non-owner, non-operator** account add the bot to a brand-new Telegram group.

2. **Expected in the group**:

   > "I can't be added to external groups by this account. Leaving."

3. The bot leaves the group. HQ receives: "Unauthorised add attempt to group *<title>* by user `<id>`; left the group."

4. No `CollabWorkspace` is persisted.

---

## Regression checks (must still work)

- HQ `/help` works unchanged.
- Existing `active` collaborative workspaces listed in `data/collaborative_workspaces.json` route messages correctly after deploying this feature.
- `/promote`, `/demote`, `/role`, `/mode`, `/close` inside a collaborative group are unaffected (still handled in `_handle_collab_command`).
- `bot.pid` single-instance lock still prevents concurrent runs.

## Troubleshooting

| Symptom | Likely cause | Check |
|---------|--------------|-------|
| Flow 1: bot message in group ignores pre-announced purpose | `create_pending` didn't run or `expected_creator_id` mismatch | `grep collab.announce` logs; verify `data/collaborative_workspaces.json` has a `pending` record with the expected creator. |
| Flow 2: silence after first reply | `[COLLAB_SETUP_COMPLETE]` didn't parse | `grep collab.setup` logs; inspect raw AI response for the marker. |
| HQ shows a group that no longer exists | Archive event was missed (bot killed mid-removal) | restart bot; next HQ list will re-derive from store. If still wrong, check `data/collaborative_workspaces.json` for stale `active` entries. |
| `[COLLAB_SEND]` reports `unknown group` for a known group | The group is `setup` or `closed`, not `active` | wait for setup-complete or reopen; `[COLLAB_SEND]` only targets active. |
