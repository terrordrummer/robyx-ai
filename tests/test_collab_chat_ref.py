"""Spec 007 — ChatRef, lifecycle event dataclasses, and CollabStore
multi-platform extensions.

Covers:

- ChatRef equality, hashing, JSON round-trip.
- Discord chat_id helpers (parse / make / round-trip).
- CollabWorkspace.platform / chat_id-str / expected_platform schema.
- from_dict legacy-int chat_id coercion.
- CollabStore.get_by_chat_id with ChatRef vs raw int.
- CollabStore.find_active_in_guild + find_active_by_platform.
- update_chat_id refuses cross-platform binds when expected_platform set.
- list_pending_for_creator honors platform filter (cross-platform collision guard).
"""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import pytest

from collaborative import (
    CollabStore,
    CollabWorkspace,
    make_discord_chat_id,
    parse_discord_chat_id,
)
from messaging.base import ChatRef, LifecycleAdded, LifecycleMigrated, LifecycleRemoved


# ── ChatRef ─────────────────────────────────────────────────────────────


class TestChatRef:
    def test_equality(self):
        a = ChatRef("telegram", "-100123")
        b = ChatRef("telegram", "-100123")
        assert a == b

    def test_inequality_across_platform(self):
        a = ChatRef("telegram", "123")
        b = ChatRef("discord", "123")
        assert a != b

    def test_hashable(self):
        a = ChatRef("telegram", "-100123")
        b = ChatRef("telegram", "-100123")
        d = {a: "x"}
        assert d[b] == "x"  # equal ChatRefs hash to the same bucket

    def test_frozen(self):
        a = ChatRef("telegram", "-100123")
        with pytest.raises(FrozenInstanceError):
            a.platform = "discord"  # type: ignore[misc]

    def test_to_dict_round_trip(self):
        a = ChatRef("discord", "111:222")
        d = a.to_dict()
        assert d == {"platform": "discord", "chat_id": "111:222"}
        b = ChatRef.from_dict(d)
        assert b == a

    def test_from_dict_coerces_int_chat_id(self):
        a = ChatRef.from_dict({"platform": "telegram", "chat_id": -100123})
        assert a.platform == "telegram"
        assert a.chat_id == "-100123"


# ── Discord chat_id helpers ────────────────────────────────────────────


class TestDiscordChatIdHelpers:
    def test_make_round_trip(self):
        s = make_discord_chat_id(111, 222)
        assert s == "111:222"
        assert parse_discord_chat_id(s) == (111, 222)

    def test_parse_malformed_no_colon(self):
        with pytest.raises(ValueError, match="malformed Discord chat_id"):
            parse_discord_chat_id("12345")

    def test_parse_non_integer_components(self):
        with pytest.raises(ValueError, match="non-integer"):
            parse_discord_chat_id("abc:def")

    def test_parse_empty_components_treated_as_non_integer(self):
        with pytest.raises(ValueError):
            parse_discord_chat_id(":222")


# ── Lifecycle event dataclasses ────────────────────────────────────────


class TestLifecycleEvents:
    def test_added_carries_chat_ref(self):
        e = LifecycleAdded(
            chat_ref=ChatRef("discord", "111:222"),
            chat_title="My Guild",
            added_by_id=999,
            added_by_name="alice",
        )
        assert e.chat_ref.platform == "discord"
        assert e.added_by_id == 999

    def test_added_optional_fields_default_to_none(self):
        e = LifecycleAdded(chat_ref=ChatRef("telegram", "-100123"))
        assert e.chat_title is None
        assert e.added_by_id is None
        assert e.added_by_name is None
        assert e.raw_event is None

    def test_removed_carries_chat_ref(self):
        e = LifecycleRemoved(chat_ref=ChatRef("telegram", "-100123"))
        assert e.chat_ref.platform == "telegram"

    def test_migrated_carries_both_refs(self):
        old = ChatRef("telegram", "-100123")
        new = ChatRef("telegram", "-100999")
        e = LifecycleMigrated(old_chat_ref=old, new_chat_ref=new)
        assert e.old_chat_ref == old
        assert e.new_chat_ref == new

    def test_added_is_frozen(self):
        e = LifecycleAdded(chat_ref=ChatRef("telegram", "-100123"))
        with pytest.raises(FrozenInstanceError):
            e.chat_title = "new"  # type: ignore[misc]


# ── CollabWorkspace schema (spec 007) ───────────────────────────────────


def _make_ws(**overrides) -> CollabWorkspace:
    defaults = {
        "id": "collab-test1",
        "name": "test-project",
        "display_name": "Test Project",
        "agent_name": "test-agent",
        "chat_id": "-1001234567890",
        "created_by": 111,
        "roles": {"111": "owner"},
    }
    defaults.update(overrides)
    return CollabWorkspace(**defaults)


class TestCollabWorkspaceSchema:
    def test_defaults_to_telegram_platform(self):
        ws = _make_ws()
        assert ws.platform == "telegram"

    def test_explicit_platform_stored(self):
        ws = _make_ws(platform="discord", chat_id="111:222")
        assert ws.platform == "discord"

    def test_expected_platform_defaults_to_none(self):
        ws = _make_ws()
        assert ws.expected_platform is None

    def test_chat_id_is_always_string_on_construction(self):
        ws = _make_ws(chat_id=-100123)
        assert ws.chat_id == "-100123"
        assert isinstance(ws.chat_id, str)

    def test_chat_ref_property(self):
        ws = _make_ws(platform="discord", chat_id="111:222")
        assert ws.chat_ref == ChatRef("discord", "111:222")

    def test_to_dict_emits_chat_id_as_str(self):
        ws = _make_ws(chat_id=-100123)
        d = ws.to_dict()
        assert d["chat_id"] == "-100123"
        assert isinstance(d["chat_id"], str)
        assert d["platform"] == "telegram"
        assert d["expected_platform"] is None

    def test_from_dict_accepts_legacy_int_chat_id(self):
        d = {
            "id": "collab-legacy",
            "name": "legacy",
            "display_name": "Legacy",
            "agent_name": "legacy",
            "chat_id": -100123,  # legacy int
            # no platform key — back-compat default applies
        }
        ws = CollabWorkspace.from_dict(d)
        assert ws.chat_id == "-100123"
        assert ws.platform == "telegram"
        assert ws.expected_platform is None

    def test_from_dict_round_trip_with_platform(self):
        d = {
            "id": "collab-discord",
            "name": "discord-ws",
            "display_name": "Discord WS",
            "agent_name": "discord-ws",
            "chat_id": "111:222",
            "platform": "discord",
            "expected_platform": "discord",
        }
        ws = CollabWorkspace.from_dict(d)
        assert ws.platform == "discord"
        assert ws.expected_platform == "discord"
        assert ws.to_dict()["chat_id"] == "111:222"


# ── CollabStore lookups ────────────────────────────────────────────────


class TestStoreLookups:
    def test_get_by_chat_id_with_chat_ref(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(platform="discord", chat_id="111:222")
        store.add(ws)
        found = store.get_by_chat_id(ChatRef("discord", "111:222"))
        assert found is ws

    def test_get_by_chat_id_legacy_int_assumes_telegram(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws()  # platform=telegram, chat_id=-1001234567890
        store.add(ws)
        # Pre-007 callers pass raw int — accepted, assumed Telegram.
        assert store.get_by_chat_id(-1001234567890) is ws

    def test_chat_id_isolation_across_platforms(self, tmp_path):
        """Numerically identical chat_ids on different platforms must not collide."""
        store = CollabStore(tmp_path / "collab.json")
        tg = _make_ws(id="c1", name="tg", agent_name="tg", chat_id="123")
        dc = _make_ws(id="c2", name="dc", agent_name="dc",
                      platform="discord", chat_id="123:456")
        store.add(tg)
        store.add(dc)
        # Lookup by Telegram chat_id finds the Telegram record only.
        assert store.get_by_chat_id(ChatRef("telegram", "123")) is tg
        # Lookup by Discord chat_id finds the Discord record only.
        assert store.get_by_chat_id(ChatRef("discord", "123:456")) is dc
        # Cross-platform lookups return None.
        assert store.get_by_chat_id(ChatRef("discord", "123")) is None
        assert store.get_by_chat_id(ChatRef("telegram", "123:456")) is None


# ── Find helpers ───────────────────────────────────────────────────────


class TestFindHelpers:
    def test_find_active_in_guild(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        a = _make_ws(id="c1", name="a", agent_name="a",
                     platform="discord", chat_id="111:222")
        b = _make_ws(id="c2", name="b", agent_name="b",
                     platform="discord", chat_id="111:333")
        c = _make_ws(id="c3", name="c", agent_name="c",
                     platform="discord", chat_id="999:444")
        store.add(a)
        store.add(b)
        store.add(c)
        in_111 = store.find_active_in_guild("111")
        assert {w.id for w in in_111} == {"c1", "c2"}
        in_999 = store.find_active_in_guild(999)  # int arg accepted
        assert {w.id for w in in_999} == {"c3"}
        in_none = store.find_active_in_guild("000")
        assert in_none == []

    def test_find_active_in_guild_ignores_closed(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        a = _make_ws(id="c1", name="a", agent_name="a",
                     platform="discord", chat_id="111:222", status="closed")
        store.add(a)
        assert store.find_active_in_guild("111") == []

    def test_find_active_in_guild_ignores_telegram(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        a = _make_ws(id="c1", name="a", agent_name="a",
                     platform="telegram", chat_id="111:222")
        store.add(a)
        assert store.find_active_in_guild("111") == []

    def test_find_active_by_platform(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        a = _make_ws(id="c1", name="a", agent_name="a")  # telegram
        b = _make_ws(id="c2", name="b", agent_name="b",
                     platform="discord", chat_id="111:222")
        c = _make_ws(id="c3", name="c", agent_name="c",
                     platform="discord", chat_id="333:444",
                     status="closed")  # closed → not active
        store.add(a)
        store.add(b)
        store.add(c)
        assert {w.id for w in store.find_active_by_platform("discord")} == {"c2"}
        assert {w.id for w in store.find_active_by_platform("telegram")} == {"c1"}


# ── update_chat_id cross-platform refusal (FR-013) ─────────────────────


class TestExpectedPlatformGate:
    def test_update_chat_id_refuses_cross_platform(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(chat_id="0", status="pending",
                      platform="discord", expected_platform="discord",
                      expected_creator_id=42)
        store.add(ws)
        # A Telegram bot-added event for the same user_id 42 must NOT
        # bind this Discord-pending workspace.
        ok = store.update_chat_id(
            ws.id, ChatRef("telegram", "-100123"), expected_creator_id=42,
        )
        assert ok is False
        # Workspace stays pending.
        reloaded = store.get(ws.id)
        assert reloaded.status == "pending"
        assert reloaded.chat_id == "0"

    def test_update_chat_id_accepts_matching_platform(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(chat_id="0", status="pending",
                      platform="discord", expected_platform="discord",
                      expected_creator_id=42)
        store.add(ws)
        ok = store.update_chat_id(
            ws.id, ChatRef("discord", "111:222"), expected_creator_id=42,
        )
        assert ok is True
        bound = store.get(ws.id)
        assert bound.status == "active"
        assert bound.platform == "discord"
        assert bound.chat_id == "111:222"

    def test_update_chat_id_no_expected_platform_allows_any(self, tmp_path):
        """Pre-007 records without expected_platform set are bindable on
        any platform (back-compat — Telegram-only environments before
        FR-013 was introduced)."""
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(chat_id="0", status="pending",
                      platform="telegram", expected_platform=None,
                      expected_creator_id=42)
        store.add(ws)
        ok = store.update_chat_id(
            ws.id, ChatRef("telegram", "-100123"), expected_creator_id=42,
        )
        assert ok is True

    def test_list_pending_for_creator_platform_filter(self, tmp_path):
        """list_pending_for_creator must honor the platform filter so
        a Telegram add event does not match a Discord-pending workspace
        with the same expected_creator_id."""
        store = CollabStore(tmp_path / "collab.json")
        discord_pending = _make_ws(
            id="c1", name="a", agent_name="a",
            chat_id="0", status="pending",
            platform="discord", expected_platform="discord",
            expected_creator_id=42,
        )
        store.add(discord_pending)
        # When platform=telegram, the Discord-pending workspace is filtered out.
        assert store.list_pending_for_creator(42, platform="telegram") == []
        # When platform=discord, it matches.
        assert [w.id for w in store.list_pending_for_creator(42, platform="discord")] == ["c1"]
        # Pre-007 callers pass no platform → match anything (back-compat).
        assert [w.id for w in store.list_pending_for_creator(42)] == ["c1"]


# ── migrate_chat_id cross-platform refusal ─────────────────────────────


class TestMigrateChatIdCrossPlatform:
    def test_refuses_cross_platform_migration(self, tmp_path):
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(id="c1", name="a", agent_name="a",
                      platform="telegram", chat_id="-100111", status="active")
        store.add(ws)
        ok = store.migrate_chat_id(
            ChatRef("telegram", "-100111"),
            ChatRef("discord", "111:222"),
        )
        assert ok is False
        # Original binding unchanged.
        assert ws.platform == "telegram"
        assert ws.chat_id == "-100111"

    def test_legacy_int_args_assumed_telegram(self, tmp_path):
        """migrate_chat_id with raw int args (pre-007 callers) must
        continue to work — both halves assumed Telegram."""
        store = CollabStore(tmp_path / "collab.json")
        ws = _make_ws(id="c1", name="a", agent_name="a",
                      platform="telegram", chat_id="-100111", status="active")
        store.add(ws)
        assert store.migrate_chat_id(-100111, -100999) is True
        assert ws.chat_id == "-100999"
