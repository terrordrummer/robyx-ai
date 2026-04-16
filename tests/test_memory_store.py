"""Tests for bot/memory_store.py — SQLite-backed memory storage."""

import sqlite3
from pathlib import Path

import pytest

import memory_store
from memory_store import (
    aggregate_active_summaries,
    append_archive_entry,
    get_connection,
    list_archive_topics,
    load_active_snapshot,
    migrate_markdown_to_sqlite,
    resolve_db_path,
    save_active_snapshot,
    search_archive,
)


# ── Connection & Schema ──


class TestGetConnection:
    def test_creates_db_file(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            assert db_path.exists()
        finally:
            conn.close()

    def test_creates_parent_dirs(self, tmp_path):
        db_path = tmp_path / "deep" / "nested" / "test.db"
        conn = get_connection(db_path)
        try:
            assert db_path.exists()
        finally:
            conn.close()

    def test_wal_mode_enabled(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal"
        finally:
            conn.close()

    def test_schema_creates_tables(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "active_snapshots" in tables
            assert "entries" in tables
            assert "fts_entries" in tables
        finally:
            conn.close()

    def test_idempotent_schema_creation(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn1 = get_connection(db_path)
        conn1.close()
        # Second open should not fail.
        conn2 = get_connection(db_path)
        conn2.close()


# ── Path resolution ──


class TestResolveDbPath:
    def test_orchestrator(self, tmp_path):
        result = resolve_db_path("robyx", "orchestrator", "/some/path", tmp_path)
        assert result == tmp_path / "memory" / "robyx.db"

    def test_orchestrator_by_name(self, tmp_path):
        result = resolve_db_path("robyx", "workspace", "/some/path", tmp_path)
        assert result == tmp_path / "memory" / "robyx.db"

    def test_specialist(self, tmp_path):
        result = resolve_db_path("reviewer", "specialist", "/some/path", tmp_path)
        assert result == tmp_path / "memory" / "reviewer.db"

    def test_workspace(self, tmp_path):
        result = resolve_db_path("myproj", "workspace", "/home/user/myproj", tmp_path)
        assert result == Path("/home/user/myproj/.robyx/memory.db")

    def test_different_agents_different_paths(self, tmp_path):
        p1 = resolve_db_path("agent-a", "specialist", "", tmp_path)
        p2 = resolve_db_path("agent-b", "specialist", "", tmp_path)
        assert p1 != p2


# ── Active snapshots ──


class TestActiveSnapshot:
    @pytest.fixture
    def conn(self, tmp_path):
        c = get_connection(tmp_path / "test.db")
        yield c
        c.close()

    def test_round_trip(self, conn):
        save_active_snapshot(conn, "test-agent", "## State\nProject is at v2.")
        result = load_active_snapshot(conn, "test-agent")
        assert "Project is at v2" in result

    def test_empty_when_no_data(self, conn):
        result = load_active_snapshot(conn, "nonexistent")
        assert result == ""

    def test_strips_whitespace(self, conn):
        save_active_snapshot(conn, "test-agent", "  content  \n\n")
        result = load_active_snapshot(conn, "test-agent")
        assert result == "content"

    def test_upsert_overwrites(self, conn):
        save_active_snapshot(conn, "test-agent", "version 1")
        save_active_snapshot(conn, "test-agent", "version 2")
        result = load_active_snapshot(conn, "test-agent")
        assert result == "version 2"

    def test_word_count_stored(self, conn):
        save_active_snapshot(conn, "test-agent", "one two three four five")
        row = conn.execute(
            "SELECT word_count FROM active_snapshots WHERE agent_name = ?",
            ("test-agent",),
        ).fetchone()
        assert row["word_count"] == 5

    def test_word_budget_flag(self, conn):
        big_content = "word " * 5001
        save_active_snapshot(conn, "test-agent", big_content)
        row = conn.execute(
            "SELECT word_count FROM active_snapshots WHERE agent_name = ?",
            ("test-agent",),
        ).fetchone()
        assert row["word_count"] > 5000

    def test_crash_safety(self, tmp_path):
        """Write, close connection uncleanly (no explicit close), reopen, verify."""
        db_path = tmp_path / "crash.db"
        conn1 = get_connection(db_path)
        save_active_snapshot(conn1, "agent", "important data")
        # Simulate unclean close — just drop the reference.
        del conn1

        conn2 = get_connection(db_path)
        try:
            result = load_active_snapshot(conn2, "agent")
            assert result == "important data"
        finally:
            conn2.close()


# ── Archive entries ──


class TestArchiveEntry:
    @pytest.fixture
    def conn(self, tmp_path):
        c = get_connection(tmp_path / "test.db")
        yield c
        c.close()

    def test_append_stores_entry(self, conn):
        row_id = append_archive_entry(
            conn, "agent", "We decided to use JWT", reason="decision",
            topic="auth", tags="security,api",
        )
        assert row_id > 0
        row = conn.execute(
            "SELECT * FROM entries WHERE id = ?", (row_id,),
        ).fetchone()
        assert row["content"] == "We decided to use JWT"
        assert row["topic"] == "auth"
        assert row["tags"] == "security,api"
        assert row["archive_reason"] == "decision"

    def test_append_multiple(self, conn):
        append_archive_entry(conn, "agent", "entry 1")
        append_archive_entry(conn, "agent", "entry 2")
        count = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE agent_name = ?",
            ("agent",),
        ).fetchone()[0]
        assert count == 2


# ── FTS5 search ──


class TestSearchArchive:
    @pytest.fixture
    def conn(self, tmp_path):
        c = get_connection(tmp_path / "test.db")
        # Seed test data.
        for i in range(20):
            append_archive_entry(
                c, "agent", f"Decision about deployment strategy iteration {i}",
                topic="deployment", tags="infra",
            )
        append_archive_entry(
            c, "agent", "Authentication uses JWT tokens with RS256",
            topic="auth", tags="security",
        )
        append_archive_entry(
            c, "agent", "Database schema uses normalized tables",
            topic="database", tags="schema",
        )
        # Different agent.
        append_archive_entry(
            c, "other-agent", "Unrelated deployment info",
            topic="deployment",
        )
        yield c
        c.close()

    def test_keyword_search(self, conn):
        results = search_archive(conn, "agent", "deployment")
        assert len(results) > 0
        assert all("deployment" in r["content"].lower() for r in results)

    def test_returns_ranked(self, conn):
        results = search_archive(conn, "agent", "JWT authentication")
        assert len(results) > 0
        assert "JWT" in results[0]["content"]

    def test_respects_limit(self, conn):
        results = search_archive(conn, "agent", "deployment", limit=5)
        assert len(results) <= 5

    def test_no_results_for_missing_term(self, conn):
        results = search_archive(conn, "agent", "kubernetes")
        assert results == []

    def test_agent_isolation(self, conn):
        results = search_archive(conn, "agent", "deployment")
        contents = [r["content"] for r in results]
        assert not any("Unrelated" in c for c in contents)

    def test_empty_query(self, conn):
        results = search_archive(conn, "agent", "")
        assert results == []

    def test_special_characters_handled(self, conn):
        results = search_archive(conn, "agent", "deployment (strategy)")
        assert len(results) > 0

    def test_performance_1000_entries(self, tmp_path):
        """Search across 1000 entries should complete quickly."""
        import time
        db_path = tmp_path / "perf.db"
        conn = get_connection(db_path)
        try:
            for i in range(1000):
                append_archive_entry(
                    conn, "agent", f"Entry {i} about topic-{i % 50}",
                    topic=f"topic-{i % 50}",
                )
            start = time.monotonic()
            results = search_archive(conn, "agent", "Entry about")
            elapsed = time.monotonic() - start
            assert len(results) > 0
            assert elapsed < 1.0  # Should be <100ms, 1s is generous.
        finally:
            conn.close()


class TestListArchiveTopics:
    def test_lists_distinct_topics(self, tmp_path):
        conn = get_connection(tmp_path / "test.db")
        try:
            append_archive_entry(conn, "agent", "e1", topic="auth")
            append_archive_entry(conn, "agent", "e2", topic="deploy")
            append_archive_entry(conn, "agent", "e3", topic="auth")
            topics = list_archive_topics(conn, "agent")
            assert topics == ["auth", "deploy"]
        finally:
            conn.close()

    def test_empty_when_no_entries(self, tmp_path):
        conn = get_connection(tmp_path / "test.db")
        try:
            topics = list_archive_topics(conn, "agent")
            assert topics == []
        finally:
            conn.close()


# ── Aggregation ──


class TestAggregateSummaries:
    def test_reads_multiple_agents(self, tmp_path):
        # Create two agent DBs.
        for name in ("agent-a", "agent-b"):
            db_path = tmp_path / f"{name}.db"
            conn = get_connection(db_path)
            save_active_snapshot(conn, name, f"State of {name}")
            conn.close()

        paths = {
            "agent-a": tmp_path / "agent-a.db",
            "agent-b": tmp_path / "agent-b.db",
        }
        result = aggregate_active_summaries(paths)
        assert "agent-a" in result
        assert "agent-b" in result
        assert "State of agent-a" in result["agent-a"]

    def test_skips_missing_db(self, tmp_path):
        paths = {"missing": tmp_path / "nonexistent.db"}
        result = aggregate_active_summaries(paths)
        assert result == {}

    def test_isolation(self, tmp_path):
        """Writing to agent A's DB must not appear in agent B's DB."""
        db_a = tmp_path / "a.db"
        db_b = tmp_path / "b.db"
        conn_a = get_connection(db_a)
        conn_b = get_connection(db_b)
        save_active_snapshot(conn_a, "a", "only in A")
        save_active_snapshot(conn_b, "b", "only in B")
        conn_a.close()
        conn_b.close()

        paths = {"a": db_a, "b": db_b}
        result = aggregate_active_summaries(paths)
        assert "only in A" in result["a"]
        assert "only in B" in result["b"]
        assert "only in A" not in result.get("b", "")


# ── Migration from markdown ──


class TestMigrateMarkdownToSqlite:
    def _setup_markdown_memory(self, mem_dir: Path):
        """Create a typical markdown memory layout."""
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / "active.md").write_text("## State\nProject is at v2.\n")
        archive = mem_dir / "archive"
        archive.mkdir()
        (archive / "2025-Q4.md").write_text(
            "\n---\n"
            "_Archived: 2025-12-01 10:00 UTC | Reason: completed_\n\n"
            "Finished the auth module\n"
            "\n---\n"
            "_Archived: 2025-12-15 14:30 UTC | Reason: superseded_\n\n"
            "Old deployment strategy\n"
        )
        (archive / "2026-Q1.md").write_text(
            "\n---\n"
            "_Archived: 2026-01-10 09:00 UTC | Reason: obsolete_\n\n"
            "Removed the legacy API\n"
        )

    def test_migrates_active(self, tmp_path):
        mem_dir = tmp_path / "memory"
        self._setup_markdown_memory(mem_dir)

        db_path = tmp_path / "test.db"
        result = migrate_markdown_to_sqlite(db_path, "agent", mem_dir)

        assert result is True
        conn = get_connection(db_path)
        try:
            active = load_active_snapshot(conn, "agent")
            assert "Project is at v2" in active
        finally:
            conn.close()

    def test_migrates_archive_entries(self, tmp_path):
        mem_dir = tmp_path / "memory"
        self._setup_markdown_memory(mem_dir)

        db_path = tmp_path / "test.db"
        migrate_markdown_to_sqlite(db_path, "agent", mem_dir)

        conn = get_connection(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE agent_name = ?",
                ("agent",),
            ).fetchone()[0]
            assert count == 3  # 2 from Q4 + 1 from Q1
        finally:
            conn.close()

    def test_renames_old_files(self, tmp_path):
        mem_dir = tmp_path / "memory"
        self._setup_markdown_memory(mem_dir)

        db_path = tmp_path / "test.db"
        migrate_markdown_to_sqlite(db_path, "agent", mem_dir)

        assert not (mem_dir / "active.md").exists()
        assert (mem_dir / "active.md.bak").exists()
        assert not list((mem_dir / "archive").glob("*.md"))
        assert len(list((mem_dir / "archive").glob("*.bak"))) == 2

    def test_idempotent(self, tmp_path):
        mem_dir = tmp_path / "memory"
        self._setup_markdown_memory(mem_dir)

        db_path = tmp_path / "test.db"
        migrate_markdown_to_sqlite(db_path, "agent", mem_dir)
        # Second run: .md files are gone (renamed to .bak), so nothing to do.
        result = migrate_markdown_to_sqlite(db_path, "agent", mem_dir)
        assert result is False

    def test_no_markdown_returns_false(self, tmp_path):
        mem_dir = tmp_path / "empty"
        mem_dir.mkdir()
        db_path = tmp_path / "test.db"
        result = migrate_markdown_to_sqlite(db_path, "agent", mem_dir)
        assert result is False

    def test_handles_missing_active(self, tmp_path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        archive = mem_dir / "archive"
        archive.mkdir()
        (archive / "2026-Q1.md").write_text(
            "\n---\n"
            "_Archived: 2026-01-01 00:00 UTC | Reason: test_\n\n"
            "Some entry\n"
        )

        db_path = tmp_path / "test.db"
        result = migrate_markdown_to_sqlite(db_path, "agent", mem_dir)
        assert result is True

    def test_handles_empty_archive(self, tmp_path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "active.md").write_text("Active stuff\n")

        db_path = tmp_path / "test.db"
        result = migrate_markdown_to_sqlite(db_path, "agent", mem_dir)
        assert result is True

        conn = get_connection(db_path)
        try:
            active = load_active_snapshot(conn, "agent")
            assert "Active stuff" in active
        finally:
            conn.close()
