"""Robyx -- Authorization layer for collaborative workspaces.

Determines what a user can do in a given context (HQ vs collaborative
group) based on their role. The bot owner is always fully authorized
everywhere.
"""

from __future__ import annotations

import logging
from typing import Any

from collaborative import CollabStore, CollabWorkspace, Role

log = logging.getLogger("robyx.authorization")


def get_user_role(
    user_id: int,
    chat_id: Any,
    collab_store: CollabStore,
    owner_id: int | None,
) -> tuple[Role | None, CollabWorkspace | None]:
    """Resolve a user's role in the context of a specific chat.

    Returns ``(role, collab_workspace)`` where:
    - For the bot owner in any collaborative workspace: ``(OWNER, ws)``
    - For the bot owner in HQ: ``(OWNER, None)``
    - For a known collaborator: ``(their role, ws)``
    - For an unknown user in a collab group: ``(None, ws)``
    - For a non-owner in HQ: ``(None, None)``

    When ``owner_id`` is ``None`` (unconfigured), no user matches the
    owner check — fail-closed. Roles still resolve via the workspace's
    explicit ``roles`` map.
    """
    ws = collab_store.get_by_chat_id(chat_id)

    is_owner_match = owner_id is not None and user_id == owner_id

    if ws is None:
        if is_owner_match:
            return Role.OWNER, None
        return None, None

    if is_owner_match:
        return Role.OWNER, ws

    role = ws.get_role(user_id)
    return role, ws


def can_send_executive(role: Role | None) -> bool:
    """Return True if the user's role allows executive instructions."""
    return role in (Role.OWNER, Role.OPERATOR)


def can_close_workspace(
    role: Role | None,
    user_id: int,
    ws: CollabWorkspace,
    owner_id: int | None = None,
) -> bool:
    """Return True if ``user_id`` may close ``ws``.

    Allowed: the workspace creator, or the bot's global owner (the
    person who runs the bot). ``role`` is accepted for API symmetry
    with the other ``can_*`` helpers; the decision is identity-based,
    not role-based, because in a collab group the OWNER role is
    scoped to that workspace.
    """
    if user_id == ws.created_by:
        return True
    if owner_id is not None and user_id == owner_id:
        return True
    return False


def can_manage_roles(role: Role | None) -> bool:
    """Return True if the user can promote/demote others."""
    return role == Role.OWNER
