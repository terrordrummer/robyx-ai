"""Tests for collaborative workspace data model and store."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from collaborative import CollabStore, CollabWorkspace, Role, validate_collab_name


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


class TestValidateCollabName:
    """P2-81 / T078a unit tests for the workspace-name validator."""

    import pytest as _pytest

    @_pytest.mark.parametrize("good", [
        "nebula",
        "astro-research",
        "collab-42",
        "a",  # single char lower bound
        "a" * 64,  # at the 64-char ceiling
        "x9-y-z",
    ])
    def test_accepts_valid_names(self, good):
        assert validate_collab_name(good) == good

    @_pytest.mark.parametrize("bad", [
        "",
        " ",  # whitespace only
        "..",
        ".",
        "../evil",
        "../../etc/passwd",
        "/absolute",
        "sub/dir",
        "sub\\dir",
        "UPPERCASE",
        "MixedCase",
        "with space",
        "with.dot",
        "with_underscore",
        "-leading-hyphen",
        "a" * 65,  # one over the ceiling
        "has\nnewline",
        "has\ttab",
        "has\x00null",
    ])
    def test_rejects_invalid_names(self, bad):
        import pytest
        with pytest.raises(ValueError, match="invalid collaborative workspace name"):
            validate_collab_name(bad)


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

    def test_corrupt_json_is_quarantined(self, tmp_path):
        """Closes Pass 1 F17 (deferred): a malformed collab file used to
        be silently overwritten by the next write, losing every
        collaborative workspace registration. Now the corrupt file is
        renamed to ``*.corrupt-<UTC>`` and the store starts empty."""
        path = tmp_path / "collab.json"
        path.write_text("{not valid json!!")

        store = CollabStore(path)
        # Store loaded nothing because the file was corrupt.
        assert store.list_all() == []
        # Original file is gone (renamed), replaced by a .corrupt-* sibling.
        assert not path.exists(), (
            "corrupt file should be quarantined, not present — otherwise "
            "the next write overwrites the original"
        )
        siblings = list(tmp_path.glob("collab.json.corrupt-*"))
        assert len(siblings) == 1
        assert siblings[0].read_text() == "{not valid json!!"

    def test_corrupt_recovers_from_snapshot(self, tmp_path):
        """When a recent updater snapshot contains a valid copy of
        collaborative_workspaces.json, the corrupt live file is
        quarantined AND the snapshot version is restored. Prevents
        total loss of the workspace registry on a crash-mid-write."""
        import tarfile
        import io
        import json as _json
        import os as _os
        import config as _cfg

        # The recovery helper is scoped to DATA_DIR, so we must use the
        # patched data dir (not the raw tmp_path) as the file's parent.
        collab_path = _cfg.DATA_DIR / "collaborative_workspaces.json"
        collab_path.parent.mkdir(parents=True, exist_ok=True)

        good_payload = {
            "collab-rec-1": {
                "id": "collab-rec-1",
                "name": "recovered-project",
                "agent_name": "collab-rec-1",
                "platform": "telegram",
                "chat_id": -100123,
                "status": "active",
                "roles": {"111": "owner"},
                "expected_creator_id": 111,
                "created_at": "2026-04-01T00:00:00+00:00",
                "activated_at": "2026-04-01T00:00:00+00:00",
            }
        }
        good_bytes = _json.dumps(good_payload).encode("utf-8")

        backups_dir = _cfg.DATA_DIR / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        snap = backups_dir / "pre-update-0.22.0-to-0.23.0-20260101T000000Z.tar.gz"
        with tarfile.open(str(snap), "w:gz") as tf:
            info = tarfile.TarInfo(name="./collaborative_workspaces.json")
            info.size = len(good_bytes)
            tf.addfile(info, io.BytesIO(good_bytes))

        # Stage corruption in the live file.
        collab_path.write_text("{not valid json!!")

        store = CollabStore(collab_path)

        # Recovery happened — the workspace from the snapshot is back.
        ws = store.get("collab-rec-1")
        assert ws is not None
        assert ws.name == "recovered-project"
        assert ws.chat_id == -100123

        # Quarantine record preserved.
        siblings = list(collab_path.parent.glob(
            collab_path.name + ".corrupt-*"
        ))
        assert len(siblings) == 1
        assert siblings[0].read_text() == "{not valid json!!"

        # Live file now holds the recovered bytes.
        restored = _json.loads(collab_path.read_text())
        assert "collab-rec-1" in restored

        # Clean up so the test doesn't pollute other tests via the
        # shared DATA_DIR fixture.
        collab_path.unlink(missing_ok=True)
        siblings[0].unlink(missing_ok=True)
        snap.unlink(missing_ok=True)

    def test_unreadable_file_degrades_gracefully(self, tmp_path):
        """If the file is present but we can't decode it (e.g. written in
        a non-UTF-8 encoding from a botched edit), the store starts empty
        and logs the failure without crashing."""
        path = tmp_path / "collab.json"
        path.write_bytes(b"\xff\xfe\x00garbage-not-utf-8")

        store = CollabStore(path)
        assert store.list_all() == []

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


class TestCreatePending:
    def test_persists_with_correct_defaults(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = store.create_pending(
            name="nebula", display_name="Nebula Research",
            agent_name="nebula",
            parent_workspace="astro-research", inherit_memory=True,
            creator_id=777,
        )
        assert ws.status == "pending"
        assert ws.chat_id == 0
        assert ws.expected_creator_id == 777
        assert ws.parent_workspace == "astro-research"
        assert ws.inherit_memory is True
        assert ws.roles == {"777": "owner"}

    def test_name_collision_raises(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        store.create_pending(
            name="nebula", display_name="Nebula",
            agent_name="nebula", parent_workspace=None,
            inherit_memory=True, creator_id=777,
        )
        import pytest
        with pytest.raises(ValueError, match="name collision"):
            store.create_pending(
                name="nebula", display_name="Other",
                agent_name="nebula", parent_workspace=None,
                inherit_memory=True, creator_id=777,
            )

    def test_zero_creator_id_raises(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        import pytest
        with pytest.raises(ValueError, match="creator_id"):
            store.create_pending(
                name="x", display_name="X", agent_name="x",
                parent_workspace=None, inherit_memory=True, creator_id=0,
            )

    def test_empty_name_raises(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        import pytest
        with pytest.raises(ValueError, match="invalid collaborative workspace name"):
            store.create_pending(
                name="", display_name="X", agent_name="x",
                parent_workspace=None, inherit_memory=True, creator_id=777,
            )

    def test_path_traversal_name_raises(self, tmp_path):
        """P2-81 defense-in-depth: create_pending must reject names that
        escape AGENTS_DIR, in case a future caller bypasses the handler-
        layer validation."""
        store = CollabStore(tmp_path / "collab.json")
        import pytest
        for bad in ("../evil", "sub/dir", "UPPER", ".", "..", "with space"):
            with pytest.raises(ValueError, match="invalid collaborative workspace name"):
                store.create_pending(
                    name=bad, display_name="X", agent_name="x",
                    parent_workspace=None, inherit_memory=True, creator_id=777,
                )

    def test_survives_roundtrip(self, tmp_path):
        path = tmp_path / "collab.json"
        store = CollabStore(path)
        store.create_pending(
            name="nebula", display_name="Nebula",
            agent_name="nebula", parent_workspace="astro-research",
            inherit_memory=False, creator_id=777,
        )
        reloaded = CollabStore(path)
        pending = reloaded.list_pending_for_creator(777)
        assert len(pending) == 1
        assert pending[0].name == "nebula"
        assert pending[0].parent_workspace == "astro-research"
        assert pending[0].inherit_memory is False


class TestFinalizeSetup:
    def test_flips_setup_to_active(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(id="c1", name="c1", agent_name="c1", status="setup")
        store.add(ws)
        assert store.finalize_setup(
            "c1", parent_workspace="astro", inherit_memory=True,
        ) is True
        assert ws.status == "active"
        assert ws.parent_workspace == "astro"
        assert ws.inherit_memory is True

    def test_rejects_non_setup_status(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(id="c1", name="c1", agent_name="c1", status="active")
        store.add(ws)
        assert store.finalize_setup(
            "c1", parent_workspace=None, inherit_memory=True,
        ) is False
        assert ws.status == "active"  # unchanged

    def test_missing_ws_returns_false(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        assert store.finalize_setup(
            "nonexistent", parent_workspace=None, inherit_memory=True,
        ) is False

    def test_persists(self, tmp_path):
        path = tmp_path / "collab.json"
        store = CollabStore(path)
        ws = _make_ws(id="c1", name="c1", agent_name="c1", status="setup")
        store.add(ws)
        store.finalize_setup(
            "c1", parent_workspace="astro", inherit_memory=False,
        )
        reloaded = CollabStore(path)
        r = reloaded.get("c1")
        assert r.status == "active"
        assert r.parent_workspace == "astro"
        assert r.inherit_memory is False


class TestMigrateChatId:
    def test_rebinds_active(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(id="c1", name="c1", agent_name="c1",
                      chat_id=-100111, status="active")
        store.add(ws)
        assert store.migrate_chat_id(-100111, -100999) is True
        assert ws.chat_id == -100999
        assert ws.status == "active"
        assert store.get_by_chat_id(-100999) is ws
        assert store.get_by_chat_id(-100111) is None

    def test_rebinds_setup(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(id="c1", name="c1", agent_name="c1",
                      chat_id=-100111, status="setup")
        store.add(ws)
        assert store.migrate_chat_id(-100111, -100999) is True
        assert ws.status == "setup"  # unchanged

    def test_rejects_unknown_old_chat(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        assert store.migrate_chat_id(-100000, -100999) is False

    def test_rejects_closed(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(id="c1", name="c1", agent_name="c1",
                      chat_id=-100111, status="closed")
        store.add(ws)
        # closed is not routable so chat_map has no entry → refuse.
        assert store.migrate_chat_id(-100111, -100999) is False

    def test_rejects_zero_new(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(id="c1", name="c1", agent_name="c1",
                      chat_id=-100111, status="active")
        store.add(ws)
        assert store.migrate_chat_id(-100111, 0) is False


class TestListForOrchestrator:
    def test_excludes_closed(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        a = _make_ws(id="c1", name="a", agent_name="a", status="active")
        b = _make_ws(id="c2", name="b", agent_name="b",
                     status="closed", chat_id=-100222)
        store.add(a)
        store.add(b)
        listed = store.list_for_orchestrator()
        assert {d["name"] for d in listed} == {"a"}

    def test_includes_active_setup_pending(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        a = _make_ws(id="c1", name="a", agent_name="a", status="active")
        b = _make_ws(id="c2", name="b", agent_name="b",
                     status="setup", chat_id=-100222)
        c = _make_ws(id="c3", name="c", agent_name="c",
                     status="pending", chat_id=0,
                     expected_creator_id=111)
        store.add(a)
        store.add(b)
        store.add(c)
        listed = store.list_for_orchestrator()
        assert {d["name"] for d in listed} == {"a", "b", "c"}
        statuses = {d["name"]: d["status"] for d in listed}
        assert statuses == {"a": "active", "b": "setup", "c": "pending"}

    def test_sort_by_created_at_desc(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        old = _make_ws(id="c-old", name="old", agent_name="old",
                       created_at=1000, status="active")
        new = _make_ws(id="c-new", name="new", agent_name="new",
                       chat_id=-100222, created_at=2000, status="active")
        store.add(old)
        store.add(new)
        listed = store.list_for_orchestrator()
        assert [d["name"] for d in listed] == ["new", "old"]

    def test_purpose_falls_back_to_display_name(self, tmp_path):
        # Agent file missing entirely — purpose should be display_name.
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(id="c1", name="nonexistent-agent",
                      agent_name="nonexistent-agent",
                      display_name="Fallback Name", status="active")
        store.add(ws)
        listed = store.list_for_orchestrator()
        assert listed[0]["purpose"] == "Fallback Name"

    def test_purpose_reads_first_content_line(self, tmp_path, monkeypatch):
        import config
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "has-purpose.md").write_text(
            "# Nebula Research\n\nCollaboration on Nebula with Alice and Bob.\n"
        )
        monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)

        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(id="c1", name="has-purpose", agent_name="has-purpose",
                      display_name="Nebula Research", status="active")
        store.add(ws)
        listed = store.list_for_orchestrator()
        assert listed[0]["purpose"] == (
            "Collaboration on Nebula with Alice and Bob."
        )
