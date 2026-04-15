"""Tests for the authorization layer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from authorization import can_close_workspace, can_manage_roles, can_send_executive, get_user_role
from collaborative import CollabStore, CollabWorkspace, Role


def _make_ws(**overrides):
    defaults = {
        "id": "collab-auth",
        "name": "auth-test",
        "display_name": "Auth Test",
        "agent_name": "auth-agent",
        "chat_id": -100999,
        "created_by": 111,
        "roles": {"111": "owner", "222": "operator", "333": "participant"},
    }
    defaults.update(overrides)
    return CollabWorkspace(**defaults)


class TestGetUserRole:
    def test_owner_in_hq(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        role, ws = get_user_role(111, -100888, store, owner_id=111)
        assert role == Role.OWNER
        assert ws is None

    def test_non_owner_in_hq(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        role, ws = get_user_role(222, -100888, store, owner_id=111)
        assert role is None
        assert ws is None

    def test_owner_in_collab(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        ws = _make_ws()
        store.add(ws)
        role, found_ws = get_user_role(111, -100999, store, owner_id=111)
        assert role == Role.OWNER
        assert found_ws is ws

    def test_operator_in_collab(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        ws = _make_ws()
        store.add(ws)
        role, found_ws = get_user_role(222, -100999, store, owner_id=111)
        assert role == Role.OPERATOR
        assert found_ws is ws

    def test_participant_in_collab(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        ws = _make_ws()
        store.add(ws)
        role, found_ws = get_user_role(333, -100999, store, owner_id=111)
        assert role == Role.PARTICIPANT
        assert found_ws is ws

    def test_unknown_in_collab(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        ws = _make_ws()
        store.add(ws)
        role, found_ws = get_user_role(999, -100999, store, owner_id=111)
        assert role is None
        assert found_ws is ws


class TestPermissionChecks:
    def test_can_send_executive(self):
        assert can_send_executive(Role.OWNER) is True
        assert can_send_executive(Role.OPERATOR) is True
        assert can_send_executive(Role.PARTICIPANT) is False
        assert can_send_executive(None) is False

    def test_can_close_workspace(self):
        ws = _make_ws()
        assert can_close_workspace(Role.OWNER, 111, ws) is True
        assert can_close_workspace(Role.OWNER, 222, ws) is False

    def test_can_close_workspace_allows_global_owner(self):
        ws = _make_ws()
        assert can_close_workspace(None, 777, ws, owner_id=777) is True
        assert can_close_workspace(None, 888, ws, owner_id=777) is False
        assert can_close_workspace(None, 777, ws, owner_id=None) is False

    def test_can_manage_roles(self):
        assert can_manage_roles(Role.OWNER) is True
        assert can_manage_roles(Role.OPERATOR) is False
        assert can_manage_roles(Role.PARTICIPANT) is False
        assert can_manage_roles(None) is False
