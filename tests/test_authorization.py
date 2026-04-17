"""Tests for the authorization layer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from authorization import (
    can_close_workspace,
    can_manage_roles,
    can_send_executive,
    get_user_role,
    is_authorised_adder,
)
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


class TestIsAuthorisedAdder:
    def test_owner_is_authorised(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        assert is_authorised_adder(777, store, owner_id=777) is True

    def test_non_owner_without_role_rejected(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        assert is_authorised_adder(888, store, owner_id=777) is False

    def test_existing_operator_is_authorised(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        store.add(_make_ws())  # 222 is operator in this ws
        assert is_authorised_adder(222, store, owner_id=777) is True

    def test_existing_participant_rejected(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        store.add(_make_ws())  # 333 is participant
        assert is_authorised_adder(333, store, owner_id=777) is False

    def test_existing_owner_in_other_ws_authorised(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        store.add(_make_ws())  # 111 is owner of auth-test
        assert is_authorised_adder(111, store, owner_id=777) is True

    def test_none_user_id_rejected(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        assert is_authorised_adder(None, store, owner_id=777) is False

    def test_none_owner_id_without_role_rejected(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        assert is_authorised_adder(888, store, owner_id=None) is False

    def test_none_owner_id_with_existing_role_authorised(self, tmp_path):
        store = CollabStore(tmp_path / "c.json")
        store.add(_make_ws())
        assert is_authorised_adder(222, store, owner_id=None) is True
