"""Spec 007.1 — conversation archive module.

Covers ``bot/conversations.py`` directly: turn logging, archive
generation, query, and edge cases (no history, corrupt JSONL, malformed
archive filenames, empty filter window).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import pytest

import conversations


@pytest.fixture(autouse=True)
def isolate_conversations_dir(tmp_path, monkeypatch):
    """Each test gets a fresh CONVERSATIONS_DIR under tmp_path."""
    monkeypatch.setattr(
        conversations, "CONVERSATIONS_DIR", tmp_path / "conversations",
    )
    return tmp_path


# ── append_turn ────────────────────────────────────────────────────────


class TestAppendTurn:
    def test_creates_file_and_writes_entry(self):
        conversations.append_turn(
            "workspace-x",
            user_text="hello",
            agent_text="hi back",
            user_id=42,
            platform="TelegramPlatform",
        )
        path = conversations._current_path("workspace-x")
        assert path.exists()
        line = path.read_text().strip()
        entry = json.loads(line)
        assert entry["user_text"] == "hello"
        assert entry["agent_text"] == "hi back"
        assert entry["user_id"] == 42
        assert entry["platform"] == "TelegramPlatform"
        assert "ts" in entry

    def test_appends_multiple_entries(self):
        for i in range(3):
            conversations.append_turn(
                "workspace-x",
                user_text="msg-%d" % i,
                agent_text="reply-%d" % i,
            )
        path = conversations._current_path("workspace-x")
        lines = path.read_text().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["user_text"] == "msg-0"
        assert json.loads(lines[2])["agent_text"] == "reply-2"

    def test_agent_isolation(self):
        conversations.append_turn("agent-a", user_text="a", agent_text="A")
        conversations.append_turn("agent-b", user_text="b", agent_text="B")
        path_a = conversations._current_path("agent-a")
        path_b = conversations._current_path("agent-b")
        assert path_a.read_text().strip() != path_b.read_text().strip()
        assert "a" in json.loads(path_a.read_text())["user_text"]
        assert "b" in json.loads(path_b.read_text())["user_text"]

    def test_unicode_round_trip(self):
        """Italian / emoji round-trip via ensure_ascii=False."""
        conversations.append_turn(
            "agent",
            user_text="ciao 🚀",
            agent_text="perfetto — andiamo",
        )
        entry = json.loads(
            conversations._current_path("agent").read_text().strip(),
        )
        assert entry["user_text"] == "ciao 🚀"
        assert entry["agent_text"] == "perfetto — andiamo"

    def test_swallows_disk_error(self, monkeypatch, caplog):
        """append_turn must never raise on the AI invocation hot path."""

        def _raise(*args, **kwargs):
            raise OSError("simulated disk full")

        # Force Path.open to raise.
        monkeypatch.setattr(Path, "open", _raise)
        with caplog.at_level("WARNING"):
            conversations.append_turn(
                "agent", user_text="x", agent_text="y",
            )
        # Logged but not raised.
        assert any("append_turn failed" in r.getMessage() for r in caplog.records)

    def test_safe_name_sanitisation(self):
        """Agent names with filesystem-hostile chars are sanitised."""
        conversations.append_turn(
            "weird/name..with::chars",
            user_text="x", agent_text="y",
        )
        # The directory is the sanitised form — no traversal escape.
        path = conversations._current_path("weird/name..with::chars")
        assert path.exists()
        # No actual filesystem traversal happened.
        assert "../" not in str(path)


# ── archive_and_clear ─────────────────────────────────────────────────


class TestArchiveAndClear:
    def test_no_history_returns_none(self):
        assert conversations.archive_and_clear("never-talked") is None

    def test_empty_log_returns_none(self):
        path = conversations._current_path("agent")
        path.parent.mkdir(parents=True)
        path.write_text("")  # empty file
        assert conversations.archive_and_clear("agent") is None

    def test_archives_to_markdown(self):
        conversations.append_turn(
            "agent", user_text="domanda 1", agent_text="risposta 1",
        )
        conversations.append_turn(
            "agent", user_text="domanda 2", agent_text="risposta 2",
        )
        archive = conversations.archive_and_clear(
            "agent", display_name="Test Agent", session_id="sess-abc",
        )
        assert archive is not None
        body = archive.read_text()
        assert "Test Agent" in body
        assert "sess-abc" in body
        assert "domanda 1" in body
        assert "risposta 1" in body
        assert "domanda 2" in body
        assert "risposta 2" in body
        # turns: 2
        assert "turns: 2" in body

    def test_clears_current_log(self):
        conversations.append_turn("agent", user_text="x", agent_text="y")
        conversations.archive_and_clear("agent")
        assert not conversations._current_path("agent").exists()

    def test_next_turn_starts_fresh_log(self):
        conversations.append_turn("agent", user_text="x", agent_text="y")
        conversations.archive_and_clear("agent")
        conversations.append_turn("agent", user_text="z", agent_text="w")
        # Only the new turn remains.
        path = conversations._current_path("agent")
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["user_text"] == "z"

    def test_corrupt_lines_are_skipped(self):
        path = conversations._current_path("agent")
        path.parent.mkdir(parents=True)
        # Mix valid + corrupt lines.
        valid_entry = json.dumps({
            "ts": "2026-05-13T10:00:00+00:00",
            "user_text": "ok", "agent_text": "fine",
        })
        path.write_text(valid_entry + "\n{not valid json\n" + valid_entry + "\n")
        archive = conversations.archive_and_clear("agent")
        assert archive is not None
        body = archive.read_text()
        assert body.count("> ok") == 2  # both valid entries rendered

    def test_only_corrupt_lines_returns_none_and_cleans_up(self):
        path = conversations._current_path("agent")
        path.parent.mkdir(parents=True)
        path.write_text("{nope\n{also-nope\n")
        assert conversations.archive_and_clear("agent") is None
        # Bad file should be cleaned up so /clear can run again cleanly.
        assert not path.exists()

    def test_two_archives_have_distinct_timestamps(self):
        conversations.append_turn("agent", user_text="a", agent_text="A")
        first = conversations.archive_and_clear("agent")
        conversations.append_turn("agent", user_text="b", agent_text="B")
        # Force a one-second gap to differentiate the archive filenames.
        import time
        time.sleep(1)
        second = conversations.archive_and_clear("agent")
        assert first.name != second.name


# ── query_archives ────────────────────────────────────────────────────


class TestQueryArchives:
    def test_empty_returns_empty_list(self):
        assert conversations.query_archives("never-talked") == []

    def test_returns_newest_first(self):
        conversations.append_turn("agent", user_text="t1", agent_text="r1")
        a1 = conversations.archive_and_clear("agent")
        import time
        time.sleep(1)
        conversations.append_turn("agent", user_text="t2", agent_text="r2")
        a2 = conversations.archive_and_clear("agent")
        results = conversations.query_archives("agent")
        assert len(results) == 2
        # Newest first — a2 is more recent than a1.
        assert Path(results[0]["path"]).name == a2.name
        assert Path(results[1]["path"]).name == a1.name

    def test_since_filter(self):
        conversations.append_turn("agent", user_text="old", agent_text="old")
        old = conversations.archive_and_clear("agent")
        import time
        time.sleep(1)
        future_threshold = datetime.now(timezone.utc) + timedelta(seconds=10)
        # Query with a `since` in the future — no archive matches.
        results = conversations.query_archives("agent", since=future_threshold)
        assert results == []

    def test_limit_caps_results(self):
        for i in range(5):
            conversations.append_turn(
                "agent", user_text="t%d" % i, agent_text="r%d" % i,
            )
            conversations.archive_and_clear("agent")
            import time
            time.sleep(1)
        results = conversations.query_archives("agent", limit=2)
        assert len(results) == 2

    def test_limit_zero_returns_empty(self):
        conversations.append_turn("agent", user_text="x", agent_text="y")
        conversations.archive_and_clear("agent")
        assert conversations.query_archives("agent", limit=0) == []

    def test_malformed_filename_skipped(self):
        # Drop a file that does not match the archive naming convention.
        adir = conversations._agent_dir("agent")
        adir.mkdir(parents=True)
        (adir / "archive-not-a-timestamp.md").write_text("garbage")
        conversations.append_turn("agent", user_text="x", agent_text="y")
        conversations.archive_and_clear("agent")
        results = conversations.query_archives("agent")
        # Only the well-formed archive is returned.
        assert len(results) == 1
        assert "garbage" not in results[0]["body"]

    def test_body_is_markdown(self):
        conversations.append_turn(
            "agent", user_text="domanda", agent_text="risposta",
        )
        conversations.archive_and_clear("agent", display_name="Atlas")
        results = conversations.query_archives("agent")
        assert "## Conversation [Atlas]" in results[0]["body"]
