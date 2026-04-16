"""Tests for bot/memory.py — agent memory system."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import memory
from memory import (
    ACTIVE_FILE,
    ARCHIVE_DIR,
    MAX_ACTIVE_WORDS,
    append_archive,
    build_memory_context,
    get_memory_dir,
    get_memory_instructions,
    has_native_claude_memory,
    is_over_budget,
    load_active,
    load_archive_index,
    save_active,
    word_count,
)


# ── Native Claude Code detection ──


class TestHasNativeClaudeMemory:
    def test_no_claude_dir(self, tmp_path):
        assert has_native_claude_memory(str(tmp_path)) is False

    def test_empty_claude_dir(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        assert has_native_claude_memory(str(tmp_path)) is False

    def test_claude_dir_with_content(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("{}")
        assert has_native_claude_memory(str(tmp_path)) is True

    def test_claude_md_without_dir(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Project instructions")
        assert has_native_claude_memory(str(tmp_path)) is True

    def test_both_claude_md_and_dir(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Instructions")
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "memory.md").write_text("stuff")
        assert has_native_claude_memory(str(tmp_path)) is True

    def test_claude_is_file_not_dir(self, tmp_path):
        (tmp_path / ".claude").write_text("not a dir")
        assert has_native_claude_memory(str(tmp_path)) is False


# ── Path resolution ──


class TestGetMemoryDir:
    def test_orchestrator_robyx(self, tmp_path):
        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = get_memory_dir("robyx", "orchestrator", "/some/path")
        assert result == tmp_path / "data" / "memory" / "robyx"

    def test_orchestrator_by_name(self, tmp_path):
        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = get_memory_dir("robyx", "workspace", "/some/path")
        assert result == tmp_path / "data" / "memory" / "robyx"

    def test_specialist(self, tmp_path):
        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = get_memory_dir("reviewer", "specialist", "/some/path")
        assert result == tmp_path / "data" / "memory" / "reviewer"

    def test_workspace(self):
        result = get_memory_dir("my-project", "workspace", "/home/user/projects/myproj")
        assert result == Path("/home/user/projects/myproj/.robyx/memory")

    def test_workspace_different_project(self):
        result = get_memory_dir("zeus", "workspace", "/Users/rpix/Workspace/zeus")
        assert result == Path("/Users/rpix/Workspace/zeus/.robyx/memory")


# ── Load ──


class TestLoadActive:
    def test_file_exists(self, tmp_path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / ACTIVE_FILE).write_text("## State\nProject is at v2.\n")
        result = load_active(mem_dir)
        assert "Project is at v2" in result

    def test_file_not_found(self, tmp_path):
        result = load_active(tmp_path / "nonexistent")
        assert result == ""

    def test_empty_file(self, tmp_path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / ACTIVE_FILE).write_text("   \n  ")
        result = load_active(mem_dir)
        assert result == ""

    def test_strips_whitespace(self, tmp_path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / ACTIVE_FILE).write_text("  content  \n\n")
        result = load_active(mem_dir)
        assert result == "content"


class TestLoadArchiveIndex:
    def test_no_archive_dir(self, tmp_path):
        result = load_archive_index(tmp_path)
        assert result == []

    def test_with_archives(self, tmp_path):
        archive = tmp_path / ARCHIVE_DIR
        archive.mkdir()
        (archive / "2025-Q1.md").write_text("old stuff")
        (archive / "2025-Q2.md").write_text("less old")
        (archive / "2026-Q1.md").write_text("recent")
        result = load_archive_index(tmp_path)
        assert result == ["2025-Q1", "2025-Q2", "2026-Q1"]

    def test_ignores_non_md(self, tmp_path):
        archive = tmp_path / ARCHIVE_DIR
        archive.mkdir()
        (archive / "2025-Q1.md").write_text("ok")
        (archive / "notes.txt").write_text("not this")
        result = load_archive_index(tmp_path)
        assert result == ["2025-Q1"]


# ── Save ──


class TestSaveActive:
    def test_writes_content(self, tmp_path):
        mem_dir = tmp_path / "memory"
        save_active(mem_dir, "## State\nAll good.")
        assert (mem_dir / ACTIVE_FILE).exists()
        assert "All good." in (mem_dir / ACTIVE_FILE).read_text()

    def test_creates_directory(self, tmp_path):
        mem_dir = tmp_path / "deep" / "nested" / "memory"
        save_active(mem_dir, "content")
        assert mem_dir.exists()
        assert (mem_dir / ACTIVE_FILE).read_text().strip() == "content"

    def test_strips_content(self, tmp_path):
        mem_dir = tmp_path / "memory"
        save_active(mem_dir, "  padded  \n\n")
        assert (mem_dir / ACTIVE_FILE).read_text() == "padded\n"


class TestAppendArchive:
    def test_creates_quarterly_file(self, tmp_path):
        append_archive(tmp_path, "Old decision about X", reason="completed")
        archive_dir = tmp_path / ARCHIVE_DIR
        assert archive_dir.exists()
        files = list(archive_dir.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "Old decision about X" in content
        assert "completed" in content
        assert "Archived:" in content

    def test_appends_to_existing(self, tmp_path):
        append_archive(tmp_path, "Entry 1", reason="done")
        append_archive(tmp_path, "Entry 2", reason="superseded")
        files = list((tmp_path / ARCHIVE_DIR).glob("*.md"))
        assert len(files) == 1  # same quarter
        content = files[0].read_text()
        assert "Entry 1" in content
        assert "Entry 2" in content

    def test_archive_dir_created(self, tmp_path):
        mem_dir = tmp_path / "fresh"
        append_archive(mem_dir, "something")
        assert (mem_dir / ARCHIVE_DIR).exists()

    @pytest.mark.parametrize(
        "month, expected_quarter",
        [
            (1, "Q1"), (2, "Q1"), (3, "Q1"),
            (4, "Q2"), (5, "Q2"), (6, "Q2"),
            (7, "Q3"), (8, "Q3"), (9, "Q3"),
            (10, "Q4"), (11, "Q4"), (12, "Q4"),
        ],
    )
    def test_quarter_naming_covers_all_months(
        self, tmp_path, month, expected_quarter
    ):
        """The quarterly file name must map each month to the right quarter.
        Previously only the current month was exercised — this parametrized
        test locks in the mapping so a future off-by-one in the ``((month-1)
        // 3) + 1`` formula is caught immediately."""
        from datetime import datetime, timezone

        fake_now = datetime(2026, month, 15, 12, 0, tzinfo=timezone.utc)

        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with patch("memory.datetime", _FakeDatetime):
            append_archive(tmp_path, "entry for %s" % expected_quarter)

        files = list((tmp_path / ARCHIVE_DIR).glob("*.md"))
        assert len(files) == 1
        assert files[0].name == "2026-%s.md" % expected_quarter

    def test_archive_header_format(self, tmp_path):
        """The header line written before each entry must contain the UTC
        timestamp and the reason — the memory instructions tell agents to
        read these, so the format is part of the contract."""
        from datetime import datetime, timezone

        fake_now = datetime(2026, 4, 9, 14, 30, tzinfo=timezone.utc)

        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with patch("memory.datetime", _FakeDatetime):
            append_archive(tmp_path, "body text", reason="superseded")

        content = (tmp_path / ARCHIVE_DIR / "2026-Q2.md").read_text()
        assert "2026-04-09 14:30 UTC" in content
        assert "Reason: superseded" in content
        assert "body text" in content
        # Entries are separated by a `---` divider for readability.
        assert "---" in content


# ── Budget ──


class TestWordCount:
    def test_simple(self):
        assert word_count("hello world foo bar") == 4

    def test_empty(self):
        assert word_count("") == 0

    def test_multiline(self):
        assert word_count("line one\nline two\nline three") == 6


class TestIsOverBudget:
    def test_under_budget(self):
        assert not is_over_budget("word " * 100)

    def test_over_budget(self):
        assert is_over_budget("word " * (MAX_ACTIVE_WORDS + 1))

    def test_exactly_at_budget(self):
        assert not is_over_budget("word " * MAX_ACTIVE_WORDS)


# ── Prompt building ──


class TestBuildMemoryContext:
    def test_no_memory(self, tmp_path):
        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = build_memory_context("robyx", "orchestrator", str(tmp_path))
        assert result == ""

    def test_with_active_memory(self, tmp_path):
        mem_dir = tmp_path / "data" / "memory" / "robyx"
        mem_dir.mkdir(parents=True)
        (mem_dir / ACTIVE_FILE).write_text("## State\nRobyx knows things.\n")

        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = build_memory_context("robyx", "orchestrator", str(tmp_path))

        assert "Agent Memory" in result
        assert "Robyx knows things." in result

    def test_with_archives_shows_index(self, tmp_path):
        mem_dir = tmp_path / "data" / "memory" / "robyx"
        mem_dir.mkdir(parents=True)
        (mem_dir / ACTIVE_FILE).write_text("Active content.\n")
        archive = mem_dir / ARCHIVE_DIR
        archive.mkdir()
        (archive / "2025-Q1.md").write_text("old")
        (archive / "2025-Q2.md").write_text("older")

        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = build_memory_context("robyx", "orchestrator", str(tmp_path))

        assert "Archive available (2 periods)" in result
        assert "2025-Q1" in result
        assert "2025-Q2" in result

    def test_workspace_memory_path(self, tmp_path):
        project_dir = tmp_path / "myproject"
        mem_dir = project_dir / ".robyx" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / ACTIVE_FILE).write_text("Project state here.\n")

        result = build_memory_context("myproject", "workspace", str(project_dir))
        assert "Project state here." in result

    def test_workspace_skips_if_native_claude_memory(self, tmp_path):
        """Workspace on a project with .claude/ → empty (Claude CLI handles it)."""
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        (project_dir / "CLAUDE.md").write_text("# Instructions")
        # Even if .robyx/memory exists, it should be skipped
        mem_dir = project_dir / ".robyx" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / ACTIVE_FILE).write_text("Should be ignored.\n")

        result = build_memory_context("myproject", "workspace", str(project_dir))
        assert result == ""

    def test_specialist_not_skipped_even_with_native(self, tmp_path):
        """Specialists always use Robyx memory regardless of work_dir."""
        (tmp_path / "CLAUDE.md").write_text("# Instructions")
        mem_dir = tmp_path / "data" / "memory" / "reviewer"
        mem_dir.mkdir(parents=True)
        (mem_dir / ACTIVE_FILE).write_text("Specialist memory.\n")

        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = build_memory_context("reviewer", "specialist", str(tmp_path))
        assert "Specialist memory." in result

    def test_robyx_not_skipped(self, tmp_path):
        """Robyx always uses Robyx memory."""
        mem_dir = tmp_path / "data" / "memory" / "robyx"
        mem_dir.mkdir(parents=True)
        (mem_dir / ACTIVE_FILE).write_text("Robyx state.\n")

        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = build_memory_context("robyx", "orchestrator", str(tmp_path))
        assert "Robyx state." in result


class TestGetMemoryInstructions:
    def test_contains_path(self, tmp_path):
        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = get_memory_instructions("robyx", "orchestrator", str(tmp_path))
        expected_path = str(tmp_path / "data" / "memory" / "robyx")
        assert expected_path in result

    def test_contains_rules(self, tmp_path):
        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = get_memory_instructions("robyx", "orchestrator", str(tmp_path))
        assert "active memory" in result.lower()
        assert "5000 words" in result
        assert "archive" in result.lower()

    def test_workspace_path(self, tmp_path):
        result = get_memory_instructions("zeus", "workspace", str(tmp_path / "zeus"))
        expected_path = str(tmp_path / "zeus" / ".robyx" / "memory")
        assert expected_path in result

    def test_workspace_skips_if_native_claude(self, tmp_path):
        """No instructions for workspace with existing Claude Code memory."""
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        (project_dir / "CLAUDE.md").write_text("# Instructions")
        result = get_memory_instructions("myproject", "workspace", str(project_dir))
        assert result == ""

    def test_specialist_always_gets_instructions(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Doesn't matter")
        with patch.object(memory, "DATA_DIR", tmp_path / "data"):
            result = get_memory_instructions("reviewer", "specialist", str(tmp_path))
        assert "active memory" in result.lower()
