"""Tests for collaborative workspace data model and store."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from collaborative import CollabStore, CollabWorkspace, Role


def _make_ws(**overrides):
    defaults = {
        "id": "collab-test1",
        "name": "test-project",
        "display_name": "Test Project",
        "agent_name": "test-agent",
        "chat_id": -1001234567890,
        "interaction_mode": "intelligent",
        "created_by": 111,
        "roles": {"111": "owner"},
    }
    defaults.update(overrides)
    return CollabWorkspace(**defaults)


class TestCollabWorkspace:
    def test_get_role_owner(self):
        ws = _make_ws()
        assert ws.get_role(111) == Role.OWNER

    def test_get_role_unknown(self):
        ws = _make_ws()
        assert ws.get_role(999) is None

    def test_set_role(self):
        ws = _make_ws()
        ws.set_role(222, Role.OPERATOR)
        assert ws.get_role(222) == Role.OPERATOR

    def test_remove_user(self):
        ws = _make_ws(roles={"111": "owner", "222": "participant"})
        assert ws.remove_user(222) is True
        assert ws.get_role(222) is None
        assert ws.remove_user(999) is False

    def test_is_owner(self):
        ws = _make_ws()
        assert ws.is_owner(111) is True
        assert ws.is_owner(222) is False

    def test_can_execute(self):
        ws = _make_ws(roles={"111": "owner", "222": "operator", "333": "participant"})
        assert ws.can_execute(111) is True
        assert ws.can_execute(222) is True
        assert ws.can_execute(333) is False
        assert ws.can_execute(999) is False

    def test_list_users(self):
        ws = _make_ws(roles={"111": "owner", "222": "participant"})
        users = ws.list_users()
        assert len(users) == 2
        assert (111, Role.OWNER) in users
        assert (222, Role.PARTICIPANT) in users

    def test_roundtrip(self):
        ws = _make_ws(parent_workspace="parent", invite_link="https://t.me/+abc")
        d = ws.to_dict()
        ws2 = CollabWorkspace.from_dict(d)
        assert ws2.id == ws.id
        assert ws2.name == ws.name
        assert ws2.agent_name == ws.agent_name
        assert ws2.chat_id == ws.chat_id
        assert ws2.parent_workspace == "parent"
        assert ws2.invite_link == "https://t.me/+abc"
        assert ws2.roles == ws.roles


class TestCollabStore:
    def test_add_and_get(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws()
        store.add(ws)
        assert store.get("collab-test1") is ws

    def test_get_by_chat_id(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws()
        store.add(ws)
        assert store.get_by_chat_id(-1001234567890) is ws
        assert store.get_by_chat_id(999) is None

    def test_get_by_agent_name(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws()
        store.add(ws)
        assert store.get_by_agent_name("test-agent") is ws
        assert store.get_by_agent_name("nonexistent") is None

    def test_close(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws()
        store.add(ws)
        assert store.close("collab-test1") is True
        assert store.get_by_chat_id(-1001234567890) is None
        assert store.get("collab-test1").status == "closed"

    def test_remove(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws()
        store.add(ws)
        assert store.remove("collab-test1") is True
        assert store.get("collab-test1") is None
        assert store.remove("nonexistent") is False

    def test_persistence(self, tmp_path):
        path = tmp_path / "collab.json"
        store = CollabStore(path)
        ws = _make_ws()
        store.add(ws)

        store2 = CollabStore(path)
        ws2 = store2.get("collab-test1")
        assert ws2 is not None
        assert ws2.name == "test-project"
        assert ws2.chat_id == -1001234567890
        assert ws2.get_role(111) == Role.OWNER

    def test_update_roles(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws()
        store.add(ws)
        assert store.update_roles("collab-test1", 222, Role.OPERATOR) is True
        assert ws.get_role(222) == Role.OPERATOR

    def test_update_interaction_mode(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws()
        store.add(ws)
        assert store.update_interaction_mode("collab-test1", "passive") is True
        assert ws.interaction_mode == "passive"
        assert store.update_interaction_mode("collab-test1", "invalid") is False

    def test_update_chat_id(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(chat_id=0, status="pending")
        store.add(ws)
        assert store.get_by_chat_id(0) is None
        assert store.update_chat_id("collab-test1", -100999) is True
        assert ws.chat_id == -100999
        assert ws.status == "active"
        assert store.get_by_chat_id(-100999) is ws

    def test_list_active(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws1 = _make_ws(id="c1", name="a", agent_name="a")
        ws2 = _make_ws(id="c2", name="b", agent_name="b", status="closed", chat_id=-100111)
        store.add(ws1)
        store.add(ws2)
        active = store.list_active()
        assert len(active) == 1
        assert active[0].id == "c1"

    def test_chat_ids_property(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws()
        store.add(ws)
        assert -1001234567890 in store.chat_ids

    def test_update_invite_link(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws()
        store.add(ws)
        assert store.update_invite_link("collab-test1", "https://t.me/+xyz") is True
        assert ws.invite_link == "https://t.me/+xyz"

    def test_setup_workspace_routable_by_chat_id(self, tmp_path):
        # C2: status="setup" must be reachable via get_by_chat_id so that
        # Flow B (in-group setup) can route the user's first reply.
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(status="setup", chat_id=-100777)
        store.add(ws)
        assert store.get_by_chat_id(-100777) is ws

    def test_list_pending_for_creator_filters_by_creator(self, tmp_path):
        # C3: pending lookup must be scoped to expected_creator_id so an
        # outsider cannot hijack a workspace provisioned for someone else.
        store = CollabStore(tmp_path / "collab.json")
        mine = _make_ws(
            id="c-mine", name="mine", agent_name="mine",
            status="pending", chat_id=0, expected_creator_id=111,
        )
        someone_else = _make_ws(
            id="c-other", name="other", agent_name="other",
            status="pending", chat_id=0, expected_creator_id=222,
        )
        store.add(mine)
        store.add(someone_else)

        for_111 = store.list_pending_for_creator(111)
        assert [w.id for w in for_111] == ["c-mine"]
        assert store.list_pending_for_creator(999) == []

    def test_expected_creator_id_persists(self, tmp_path):
        path = tmp_path / "collab.json"
        store = CollabStore(path)
        ws = _make_ws(
            status="pending", chat_id=0, expected_creator_id=555,
        )
        store.add(ws)
        reloaded = CollabStore(path)
        assert reloaded.get("collab-test1").expected_creator_id == 555

    def test_purge_closed_drops_only_closed(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        active = _make_ws(id="c1", name="a", agent_name="a", status="active")
        closed1 = _make_ws(
            id="c2", name="b", agent_name="b",
            status="closed", chat_id=-100222,
        )
        closed2 = _make_ws(
            id="c3", name="c", agent_name="c",
            status="closed", chat_id=-100333,
        )
        store.add(active)
        store.add(closed1)
        store.add(closed2)

        assert store.purge_closed() == 2
        assert {w.id for w in store.list_all()} == {"c1"}
        # Idempotent.
        assert store.purge_closed() == 0

    def test_list_all_returns_every_status(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        a = _make_ws(id="c1", name="a", agent_name="a", status="active")
        b = _make_ws(id="c2", name="b", agent_name="b", status="setup", chat_id=-100222)
        c = _make_ws(id="c3", name="c", agent_name="c", status="closed", chat_id=-100333)
        store.add(a)
        store.add(b)
        store.add(c)
        assert {w.id for w in store.list_all()} == {"c1", "c2", "c3"}
