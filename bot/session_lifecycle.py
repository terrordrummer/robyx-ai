"""Robyx — agent AI-CLI session lifecycle helpers.

The Claude Code CLI bakes the system prompt into a session at creation
time and ignores ``--append-system-prompt`` on ``--resume``. So whenever a
release modifies any of the following, the affected agents must start a
fresh session for the new instructions to actually take effect:

- the system prompts in :mod:`bot.config`
  (``ROBYX_SYSTEM_PROMPT`` / ``WORKSPACE_AGENT_SYSTEM_PROMPT`` /
  ``FOCUSED_AGENT_SYSTEM_PROMPT``)
- the per-agent brief loader in :mod:`bot.ai_invoke`
  (``_load_agent_instructions``)
- an individual agent's brief at ``agents/<name>.md``
- a specialist's brief at ``specialists/<name>.md``

Otherwise the new instructions are silently dropped on every turn — which
is exactly the regression that bit v0.14 → v0.15 and required the
``0.15.0-reset-sessions-for-reminder-skill`` migration.

v0.15.1 promoted that pattern from a per-release migration to an
updater-driven mechanism: :func:`bot.updater.apply_update` computes the
git diff between the pre-pull and post-pull commit and resets the
affected agents before the bot restarts.

v0.15.2 fixes the **AgentManager-clobber** bug that made the v0.15.0
migration and the v0.15.1 updater path ineffective in production: both
mutated ``data/state.json`` on disk while the running bot held the
pre-mutation copy in memory, so the next ``save_state()`` call from any
interaction silently overwrote the reset. The fix is to route every
reset through :meth:`AgentManager.reset_sessions`, which mutates
``self.agents`` in place and then persists. This module no longer owns
the file I/O — it owns only the *decision* of which agents to reset
based on a diff. The actual mutation is the AgentManager's job.

Reset granularity:

- A change to one of :data:`GLOBAL_INVALIDATION_FILES` invalidates **all**
  agents (the prompt is global).
- A change to ``agents/<name>.md`` invalidates **only that workspace
  agent**.
- A change to ``specialists/<name>.md`` invalidates **only that
  specialist**.
- Anything else is ignored (logic-only changes do not need a session
  reset — the new Python code is picked up by the process restart that
  follows ``apply_update``).
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("robyx.session_lifecycle")


# Files whose change invalidates EVERY agent's session, because the change
# affects the system prompt that is baked into every agent's CLI session at
# creation time.
GLOBAL_INVALIDATION_FILES: frozenset[str] = frozenset({
    "bot/config.py",       # ROBYX_/WORKSPACE_/FOCUSED_AGENT_SYSTEM_PROMPT live here
    "bot/ai_invoke.py",    # _load_agent_instructions lives here
})

# Path patterns that map to a single agent / specialist by name. Anchored
# to the start so subdirectories like ``agents/legacy/foo.md`` do not
# accidentally match.
_AGENT_PATH_PATTERN = re.compile(r"^agents/([^/]+)\.md$")
_SPECIALIST_PATH_PATTERN = re.compile(r"^specialists/([^/]+)\.md$")


def agents_to_invalidate(
    changed_paths: list[str],
    known_agent_names: set[str],
) -> set[str] | None:
    """Decide which agents must have their sessions invalidated.

    Pure function — no I/O, no manager dependency. Used by both the
    updater path and the unit tests.

    Args:
      changed_paths: repo-relative paths from a ``git diff --name-only``
        between the pre-update and post-update commits.
      known_agent_names: the set of agents currently registered in
        ``manager.agents`` (used to ignore briefs for agents that no
        longer exist or were renamed away).

    Returns:
      ``None`` — meaning "every known agent" — if any path in
      :data:`GLOBAL_INVALIDATION_FILES` is in ``changed_paths``.
      Otherwise a (possibly empty) set of agent names whose individual
      brief was modified.
    """
    if any(p in GLOBAL_INVALIDATION_FILES for p in changed_paths):
        return None

    affected: set[str] = set()
    for p in changed_paths:
        m = _AGENT_PATH_PATTERN.match(p)
        if m:
            name = m.group(1)
            if name in known_agent_names:
                affected.add(name)
            continue
        m = _SPECIALIST_PATH_PATTERN.match(p)
        if m:
            name = m.group(1)
            if name in known_agent_names:
                affected.add(name)
    return affected


def invalidate_sessions_via_manager(
    manager,
    changed_paths: list[str],
) -> list[str]:
    """Decide what to reset and ask the manager to do it.

    The high-level entry point used by :func:`bot.updater.apply_update`.
    Routes the reset through :meth:`AgentManager.reset_sessions` so the
    in-memory and on-disk state stay in sync. **Never** mutate
    ``state.json`` outside of the AgentManager — see the v0.15.0/v0.15.1
    incident for the consequences.

    Args:
      manager: the live :class:`AgentManager`. May be ``None`` (defensive
        — the updater logs a warning and skips invalidation in that case).
      changed_paths: repo-relative paths reported by
        ``git diff --name-only`` between the pre-update and post-update
        commits.

    Returns:
      The sorted list of agent names that were actually reset. Empty if:
      ``manager`` is None, ``changed_paths`` is empty, the manager has no
      agents, or the diff did not touch any file relevant to the session
      lifecycle.
    """
    if manager is None:
        log.info("invalidate_sessions_via_manager: no manager — skipping")
        return []
    if not changed_paths:
        return []

    known = set(manager.agents.keys())
    if not known:
        return []

    target = agents_to_invalidate(changed_paths, known)
    if target is None:
        # Global invalidation — every known agent.
        scope = "global"
        names_reset = manager.reset_sessions(None)
    else:
        if not target:
            return []
        scope = "per-agent"
        names_reset = manager.reset_sessions(target)

    log.info(
        "Invalidated AI-CLI sessions (%s) for %d agent(s): %s",
        scope, len(names_reset), ", ".join(names_reset),
    )
    return names_reset
