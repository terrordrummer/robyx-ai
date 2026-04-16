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

    # Commands (usage and progress)
    "reset_usage": "Usage: /reset <name>",
    "update_checking_manual": "Checking for pending update...",

    # Time formatting
    "time_now": "now",
    "time_minutes": "%dm ago",
    "time_hours": "%dh ago",
    "time_days": "%dd ago",
}
