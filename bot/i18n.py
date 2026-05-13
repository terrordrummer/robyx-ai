"""Robyx — UI strings (English).

All user-facing text in one place for easy localization.
To translate, copy this file and swap the values.
"""

STRINGS = {
    # General
    "unauthorized": "Unauthorized.",
    "empty_message": "Empty message.",
    "unmapped_topic": (
        "This topic is not bound to any workspace agent.\n\n"
        "Go to the Headquarters and ask Robyx to create a workspace, "
        "or use `/workspaces` to see the registered ones."
    ),
    "bot_alive": "Robyx alive — %d agents%s",

    # Agents
    "no_agents": "No active agents.",
    "agents_title": "Active agents:\n",
    "agent_spawned": "Agent *%s* activated\nDir: `%s`",
    "agent_not_found": "Agent '%s' not found. See /workspaces.",
    "agent_killed": "Agent *%s* deactivated.",
    "agent_kill_failed": "Cannot deactivate '%s'.",
    "agent_reset": "Session *%s* reset. Fresh conversation.",

    # Focus
    "focus_active": "Focus active: *%s*\nUse `/focus off` to return to Robyx.",
    "focus_none": "No focus active. Usage: /focus <name> or /focus off",
    "focus_on": "Focus → *%s*\nAll messages go to %s.",
    "focus_off": "Back to *Robyx* on the Headquarters. All messages go to the orchestrator.",
    "focus_off_was": "Focus off%s. Messages go to Robyx.",

    # Workspaces
    "workspaces_title": "Workspaces:\n",
    "no_workspaces": "No workspaces. Talk to Robyx on the Headquarters to create one.",
    "workspace_created": "Workspace *%s* created in topic #%s",
    "workspace_closed": "Workspace *%s* closed.",

    # Specialists
    "specialists_title": "Cross-functional agents:\n",
    "no_specialists": "No specialists configured.",
    "specialist_created": "Specialist *%s* created in topic #%s",

    # AI invocation
    "ai_timeout": "[Timeout: no response within %ds]",
    "ai_idle_timeout": "[Timeout: no output from agent for %ds]",
    "ai_empty": "[Empty response]",
    "ai_no_response": "[No response from AI]",
    "ai_error": "AI Error: %s",
    "rate_limited": "Rate limit reached — retry in a few minutes.",
    "network_error": "Network error — cannot reach AI backend.",
    "permission_denied": "Permission denied for this operation.",
    "session_expired": "Session expired — use `/reset <agent>` for a fresh conversation.",

    # Scheduler
    "scheduler_dispatched": "Dispatched: %s",
    "scheduler_skipped": "Skipped: %s (%s)",
    "scheduler_idle": "No tasks due this cycle.",

    # Delegation
    "delegation_sent": "Delegating → *%s*: _%s_",
    "delegation_agent_missing": "Agent *%s* not active. Activate it first.",
    "delegation_result": "*%s*:\n%s",

    # Service
    "config_updated": "Updated `.env`: %s",
    "restart_pending": "Restarting service...",

    # Voice
    "voice_no_key": "Cannot transcribe voice messages — OpenAI API key is missing.\n\nAdd `OPENAI_API_KEY=sk-...` to `.env` and restart the bot.",
    "voice_error": "Failed to transcribe voice message (error: %s). Please try again shortly.",
    "voice_too_large": "Voice message is too large to transcribe (%d MB, cap %d MB). Please record a shorter clip.",
    "voice_transcript": "%s",

    # Help
    "help_text": (
        "*Robyx* — AI Agent Staff\n\n"
        "*Commands:*\n"
        "/workspaces — Show active workspaces and their status\n"
        "/specialists — Show cross-functional specialist agents\n"
        "/status — System overview (agents, focus, scheduler)\n"
        "/focus `<name|off>` — Route all messages to a specific agent (or disable)\n"
        "/reset `<name>` — Reset an agent's session (fresh conversation)\n"
        "/clear — Archive the current conversation and reset the session "
        "(use inside any non-HQ topic — workspace, collaborative, specialist)\n"
        "/ping — Quick health check\n"
        "/checkupdate — Check if a new Robyx version is available\n"
        "/doupdate — Download and apply a pending update\n\n"
        "Send any message in Headquarters to talk to the orchestrator. "
        "Messages in workspace topics go directly to that agent."
    ),

    # Updates
    "update_available": (
        "*Update available: v%s → v%s*\n\n"
        "%s\n\n"
        "Use /doupdate to apply."
    ),
    "update_available_breaking": (
        "*Update available: v%s → v%s* (BREAKING)\n\n"
        "%s\n\n"
        "This release has breaking changes and requires manual intervention.\n"
        "See `releases/%s.md` for details."
    ),
    "update_available_incompatible": (
        "*Update available: v%s → v%s*\n\n"
        "Cannot update directly — minimum compatible version is v%s.\n"
        "Manual upgrade required."
    ),
    "update_checking": "Checking for updates...",
    "update_none": "Already running the latest version (v%s).",
    "update_applying": "Applying update to v%s...",
    "update_migration": "Running migration steps...",
    "update_migration_step": "Migration: `%s`",
    "update_success": "Updated to *v%s*. Restarting...",
    "update_failed": "Update failed: %s\nRolled back to v%s.",
    "update_fetch_error": "Failed to check for updates: %s",
    "update_no_pending": "No pending update. Use /doupdate after an update notification.",
    "update_auto_applying": (
        "*Auto-update: v%s → v%s*\n\n"
        "%s\n\n"
        "Applying automatically..."
    ),
    "update_auto_failed": "Auto-update to v%s failed: %s\nUse `/doupdate` to retry manually.",

    # Collaborative workspaces
    "collab_promote_usage": "Usage: /promote <user_id>",
    "collab_demote_usage": "Usage: /demote <user_id>",
    "collab_mode_usage": "Usage: /mode <intelligent|passive>",
    "collab_not_owner": "Only the owner can do this.",
    "collab_user_not_found": "User %s is not in this workspace.",
    "collab_promoted": "User %s promoted to *%s*.",
    "collab_demoted": "User %s demoted to *%s*.",
    "collab_cannot_change_owner": "Cannot change the owner's role.",
    "collab_already_role": "User %s is already *%s*.",
    "collab_mode_changed": "Interaction mode changed to *%s*.",
    "collab_roles_title": "*Workspace roles:*\n",
    "collab_close_confirm": "Collaborative workspace *%s* closed.",
    "collab_close_denied": "Only the workspace creator can close it.",
    "collab_no_users": "No users registered in this workspace.",

    # External group wiring (feature 003)
    "collab_unauthorised_adder": (
        "I can't be added to external groups by this account. Leaving."
    ),
    "collab_unauthorised_adder_hq": (
        "Unauthorised add attempt to group *%s* (chat_id `%s`) by user `%s`; "
        "left the group."
    ),
    # Spec 007: ``collab_unsupported_platform_discord`` removed — Discord
    # is now a first-class collaborative-workspace platform. The Slack
    # variant remains until spec 008 closes the corresponding gap.
    "collab_unsupported_platform_slack": (
        "External collaborative groups are not yet supported on Slack. "
        "Use Telegram for external groups. The bot will take no further "
        "action here."
    ),
    # Spec 007 — Discord audit-log fallback advisories. The lifecycle
    # handler posts ``discord_audit_log_unavailable`` in the first
    # writable channel of a guild when the audit-log inviter lookup
    # fails (Forbidden / empty / sustained errors). The user can then
    # claim a pre-announced workspace via ``/im-the-owner <name>``.
    "discord_audit_log_unavailable": (
        "I couldn't determine who added me to this Discord guild. Please "
        "ensure I have the *View Audit Log* permission, or use "
        "`/im-the-owner <workspace-name>` in any channel of this guild "
        "to manually claim a pending workspace."
    ),
    "discord_audit_log_empty": (
        "I couldn't find the audit-log entry for my recent add to this "
        "guild. If you pre-announced a workspace for this guild, type "
        "`/im-the-owner <workspace-name>` here to claim it."
    ),
    "discord_no_writable_channel_hq": (
        "I was added to Discord guild *%s* (`%s`) but I have no writable "
        "text channel there — I have left the guild."
    ),
    "collab_announce_ok": "[COLLAB_ANNOUNCE ok: name=%s]",
    "collab_announce_error": "[COLLAB_ANNOUNCE error: %s]",
    "collab_announce_rejected": "[COLLAB_ANNOUNCE rejected: %s]",
    "collab_send_ok": "[COLLAB_SEND ok: %s]",
    "collab_send_error": "[COLLAB_SEND error: %s]",
    "collab_send_rejected": "[COLLAB_SEND rejected: %s]",
    "collab_setup_complete_hq": (
        "*Collaborative workspace setup complete*\n\n"
        "Workspace *%s* (`%s`) is now active.\n"
        "Purpose: %s\n"
        "Inherits: %s (memory: %s)\n"
        "chat_id: `%s`"
    ),
    "collab_setup_failed_group": (
        "Couldn't save setup — please try again."
    ),
    "collab_bot_added_hq_pending": (
        "I've been added to group *%s* (chat_id `%s`). "
        "Setup conversation in progress."
    ),
    "collab_bot_added_hq_matched": (
        "*Collaborative workspace configured*\n\n"
        "Workspace *%s* is now linked to group _%s_ (chat_id `%s`).\n"
        "Purpose: %s%s"
    ),
    "collab_bot_removed_hq": (
        "Collaborative workspace *%s* has been closed (bot removed from group)."
    ),
    "collab_migrated_hq": (
        "Workspace *%s* migrated to new chat_id `%s`."
    ),
    "collab_welcome_pending": (
        "*%s* — collaborative workspace is ready.\n\n"
        "Purpose: %s\n\n"
        "I'm the agent for this workspace. Owner and operators can give me "
        "executive instructions; other participants can talk and I'll help "
        "when appropriate."
    ),

    # /im-the-owner manual claim (spec 007 — Discord audit-log fallback)
    "im_the_owner_usage": (
        "Usage: `/im-the-owner <workspace-name>`"
    ),
    "im_the_owner_unknown_workspace": (
        "I don't know about a pending workspace named *%s*. List pending "
        "workspaces from HQ to confirm the name."
    ),
    "im_the_owner_already_bound": (
        "Workspace *%s* is already bound (status `%s`). Manual claim is "
        "only allowed for pending workspaces."
    ),
    "im_the_owner_platform_mismatch": (
        "Workspace *%s* was pre-announced for *%s*, not Discord. Manual "
        "claim is refused."
    ),
    "im_the_owner_creator_mismatch": (
        "Workspace *%s* was pre-announced for a different user. Ask the "
        "original announcer to claim it, or recreate the workspace pinned "
        "to your Discord id."
    ),
    "im_the_owner_success": (
        "Workspace *%s* is now active in this channel. HQ has been notified."
    ),

    # Commands (usage and progress)
    "reset_usage": "Usage: /reset <name>",

    # /clear — spec 007.1 conversation archive + session reset.
    "clear_usage": (
        "Usage: type `/clear` inside a workspace, collaborative, or specialist "
        "topic to archive the current conversation and start fresh. From HQ "
        "you can also pass an explicit name: `/clear <agent-name>`."
    ),
    "clear_refused_in_hq": (
        "`/clear` is not available in the HQ orchestrator topic — it stays "
        "session-continuous on purpose. Use `/clear` inside a workspace, "
        "collaborative group, or specialist topic instead."
    ),
    "clear_archived": (
        "Session reset for *%s*. The previous conversation was archived to "
        "`data/conversations/%s`. The next message starts a fresh session."
    ),
    "clear_no_history": (
        "Session reset for *%s*. No history to archive (no turns recorded "
        "since the last reset)."
    ),
    "archive_unknown_agent": (
        "Unknown agent *%s* — no conversation archive available."
    ),
    "update_checking_manual": "Checking for pending update...",

    # Continuous-task macro (feature 004)
    "continuous_task_created": (
        "Continuous task *%s* created (topic #%s, branch `%s`)."
    ),
    "continuous_task_error_malformed": (
        "Continuous task not created — the setup block was incomplete."
    ),
    "continuous_task_error_bad_json": (
        "Continuous task not created — the program payload could not be parsed."
    ),
    "continuous_task_error_missing_field": (
        "Continuous task not created — required field missing: %s."
    ),
    "continuous_task_error_path_denied": (
        "Continuous task not created — the requested work directory is outside the workspace."
    ),
    "continuous_task_error_name_taken": (
        "Continuous task *%s* not created — a task with that name is already "
        "registered. To reuse the name, run `[DELETE_TASK name=\"%s\"]` first "
        "(archives the topic and frees the name), or pick a different name."
    ),
    "continuous_task_error_permission_denied": (
        "Continuous task not created — this agent is not authorised to create one here."
    ),
    "continuous_task_error_downstream": (
        "Continuous task not created — an internal error prevented setup."
    ),

    # UPDATE_PLAN macro
    "update_plan_ok": (
        "Plan for continuous task *%s* updated."
    ),
    "update_plan_error_malformed": (
        "Plan update skipped — the update block was incomplete."
    ),
    "update_plan_error_bad_json": (
        "Plan update skipped — the program payload could not be parsed."
    ),
    "update_plan_error_bad_field": (
        "Plan update skipped — invalid value for field `%s`."
    ),
    "update_plan_error_not_found": (
        "Plan update skipped — no continuous task named *%s* in this workspace."
    ),
    "update_plan_error_downstream": (
        "Plan update skipped — an internal error prevented the update."
    ),

    # Time formatting
    "time_now": "now",
    "time_minutes": "%dm ago",
    "time_hours": "%dh ago",
    "time_days": "%dd ago",
}
