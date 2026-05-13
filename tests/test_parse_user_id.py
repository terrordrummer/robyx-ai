"""Spec 007 Phase 5 — cross-platform mention parser.

Covers ``_parse_user_id`` for all 6 documented input shapes (FR-018):

- Discord ``<@123>`` and ``<@!123>`` (nickname-aware) → int.
- Slack ``<@U12345>`` → str (reserved for spec 008 but accepted now).
- Telegram legacy: bare numeric, ``@123``, ``@username`` (alpha — None).
- Garbage / empty / whitespace → None.

Plus a regression: ``_parse_user_id`` returns a value that
:meth:`CollabWorkspace.set_role` / ``get_role`` can use as a dict key
without ``TypeError`` (both ``int`` and ``str`` are accepted after spec 007
Phase 1's type-hint widening).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import pytest

from collaborative import CollabStore, CollabWorkspace, Role
from handlers import make_handlers


@pytest.fixture
def store(tmp_path):
    return CollabStore(path=tmp_path / "data" / "collab.json")


@pytest.fixture
def parse(agent_manager, claude_backend, store):
    handlers = make_handlers(agent_manager, claude_backend, store)
    return handlers["_parse_user_id"]


# ── Discord mentions ──────────────────────────────────────────────────


class TestDiscordMentions:
    def test_plain_mention(self, parse):
        assert parse("<@456789012345678901>") == 456789012345678901

    def test_nickname_mention(self, parse):
        # Nickname-aware Discord clients emit ``<@!id>``; identical semantics.
        assert parse("<@!456789012345678901>") == 456789012345678901

    def test_short_numeric_mention(self, parse):
        assert parse("<@123>") == 123
        assert parse("<@!123>") == 123

    def test_returns_int_not_str(self, parse):
        result = parse("<@123>")
        assert isinstance(result, int)


# ── Slack mentions ────────────────────────────────────────────────────


class TestSlackMentions:
    def test_slack_user_id_returns_str(self, parse):
        # Slack ids are opaque strings like ``"U01ABC..."`` — they are NOT
        # numeric and must round-trip as str.
        result = parse("<@U01ABC123>")
        assert result == "U01ABC123"
        assert isinstance(result, str)

    def test_slack_user_id_alphanumeric(self, parse):
        assert parse("<@UDEADBEEF1>") == "UDEADBEEF1"

    def test_workspace_or_group_ids_not_supported(self, parse):
        # Slack workspace ids start with W; group/channel ids with G/C; we
        # only accept user ids (U prefix). Other prefixes fall through.
        assert parse("<@W123>") is None


# ── Telegram legacy ───────────────────────────────────────────────────


class TestTelegramLegacy:
    def test_bare_numeric(self, parse):
        assert parse("123") == 123

    def test_at_prefixed_numeric(self, parse):
        assert parse("@123") == 123

    def test_at_prefixed_username_returns_none(self, parse):
        # Alphanumeric @username has no numeric id we can resolve — return None.
        assert parse("@username") is None

    def test_negative_numeric(self, parse):
        # Telegram supergroup ids are negative — accept them as int.
        assert parse("-1001234567890") == -1001234567890


# ── Robustness ────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_string(self, parse):
        assert parse("") is None

    def test_whitespace_only(self, parse):
        assert parse("   ") is None

    def test_none_text_does_not_crash(self, parse):
        assert parse(None) is None  # type: ignore[arg-type]

    def test_garbage(self, parse):
        assert parse("not a user id") is None
        assert parse("<@>") is None
        assert parse("<@@123>") is None
        assert parse("<@123") is None  # unclosed

    def test_whitespace_around_valid_input(self, parse):
        # Strips outer whitespace.
        assert parse("  <@123>  ") == 123
        assert parse("  @456  ") == 456


# ── Integration: parsed id round-trips through role storage ────────────


class TestRoleStorageIntegration:
    """Confirm the parser output is directly usable with
    :class:`CollabWorkspace.set_role` / ``get_role`` regardless of
    platform — closes the spec 007 Phase 1 type-hint widening contract."""

    def test_discord_int_round_trip(self, parse):
        uid = parse("<@456789012345678901>")
        ws = CollabWorkspace(
            id="c1", name="x", display_name="X", agent_name="x",
            chat_id="111:222", platform="discord",
        )
        ws.set_role(uid, Role.OPERATOR)
        assert ws.get_role(uid) == Role.OPERATOR
        # Stored key is the canonical string form.
        assert "456789012345678901" in ws.roles

    def test_slack_str_round_trip(self, parse):
        uid = parse("<@U01ABC123>")
        ws = CollabWorkspace(
            id="c2", name="x", display_name="X", agent_name="x",
            chat_id="T01:C01", platform="slack",
        )
        ws.set_role(uid, Role.OPERATOR)
        assert ws.get_role(uid) == Role.OPERATOR
        assert "U01ABC123" in ws.roles

    def test_telegram_int_round_trip(self, parse):
        uid = parse("123")
        ws = CollabWorkspace(
            id="c3", name="x", display_name="X", agent_name="x",
            chat_id="-100123", platform="telegram",
        )
        ws.set_role(uid, Role.OPERATOR)
        assert ws.get_role(uid) == Role.OPERATOR
