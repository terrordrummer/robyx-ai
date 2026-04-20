"""Tests for bot/updater.py — auto-update system."""

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

import updater
import config as cfg


# ── Helpers ──


@pytest.fixture(autouse=True)
def _patch_updater_paths(tmp_path, _patch_env):
    """Patch updater module's local copies of config paths."""
    with patch.object(updater, "VERSION_FILE", tmp_path / "VERSION"), \
         patch.object(updater, "UPDATES_STATE_FILE", tmp_path / "data" / "updates.json"), \
         patch.object(updater, "DATA_DIR", tmp_path / "data"), \
         patch.object(updater, "PROJECT_ROOT", tmp_path), \
         patch.object(updater, "RELEASES_DIR", tmp_path / "releases"):
        (tmp_path / "VERSION").write_text("0.1.0\n")
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "releases").mkdir(exist_ok=True)
        # Fake venv pip binary so apply_update's preflight check does not
        # early-exit. The actual pip invocation is always patched via
        # updater.asyncio.create_subprocess_exec in the individual tests.
        (tmp_path / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".venv" / "bin" / "pip").write_text("#!/bin/sh\nexit 0\n")
        (tmp_path / "bot").mkdir(exist_ok=True)
        (tmp_path / "bot" / "requirements.txt").write_text("dummy==1.0\n")
        yield


@pytest.fixture
def stub_safety_helpers():
    """v0.20.14 wired ``_snapshot_data_dir`` + ``_post_update_smoke_test``
    into ``apply_update``. Legacy ``apply_update`` tests pre-date both
    helpers and would break or run real subprocesses if either fired —
    opt them out via ``pytestmark = pytest.mark.usefixtures(...)`` on
    the affected classes. Tests that exercise the safety path don't
    request this fixture and see the real helpers."""
    with patch.object(updater, "_snapshot_data_dir", return_value=None), \
         patch.object(updater, "_post_update_smoke_test", new=AsyncMock(return_value=(True, ""))):
        yield


def _write_state(tmp_path, state: dict):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "updates.json").write_text(json.dumps(state))


def _read_state(tmp_path) -> dict:
    return json.loads((tmp_path / "data" / "updates.json").read_text())


# ── get_current_version ──


class TestGetCurrentVersion:
    def test_reads_version_file(self, tmp_path):
        (tmp_path / "VERSION").write_text("0.1.0\n")
        assert updater.get_current_version() == "0.1.0"

    def test_strips_whitespace(self, tmp_path):
        (tmp_path / "VERSION").write_text("  1.2.3  \n\n")
        assert updater.get_current_version() == "1.2.3"


# ── _load_state ──


class TestLoadState:
    def test_file_exists_valid_json(self, tmp_path):
        state = {"notified_versions": ["0.2.0"], "last_check": "2025-01-01T00:00:00"}
        _write_state(tmp_path, state)
        result = updater._load_state()
        assert result["notified_versions"] == ["0.2.0"]
        assert result["last_check"] == "2025-01-01T00:00:00"

    def test_file_does_not_exist(self, tmp_path):
        state_file = tmp_path / "data" / "updates.json"
        if state_file.exists():
            state_file.unlink()
        result = updater._load_state()
        assert result == {
            "notified_versions": [],
            "last_check": None,
            "last_update": None,
            "update_history": [],
        }

    def test_file_with_invalid_json(self, tmp_path):
        (tmp_path / "data" / "updates.json").write_text("NOT JSON {{{")
        result = updater._load_state()
        assert result == {
            "notified_versions": [],
            "last_check": None,
            "last_update": None,
            "update_history": [],
        }


# ── _save_state ──


class TestSaveState:
    def test_writes_json(self, tmp_path):
        state = {"notified_versions": ["0.3.0"], "last_check": "now"}
        updater._save_state(state)
        written = json.loads((tmp_path / "data" / "updates.json").read_text())
        assert written["notified_versions"] == ["0.3.0"]

    def test_creates_parent_dirs(self, tmp_path):
        import shutil
        data_dir = tmp_path / "data"
        if data_dir.exists():
            shutil.rmtree(data_dir)
        updater._save_state({"test": True})
        assert (tmp_path / "data" / "updates.json").exists()


# ── _parse_release_notes ──


class TestParseReleaseNotes:
    def test_full_frontmatter(self):
        text = (
            "---\n"
            "version: 0.2.0\n"
            "min_compatible: 0.1.0\n"
            "breaking: true\n"
            "requires_migration: true\n"
            "---\n"
            "Some release body.\n"
        )
        result = updater._parse_release_notes(text)
        assert result["version"] == "0.2.0"
        assert result["min_compatible"] == "0.1.0"
        assert result["breaking"] is True
        assert result["requires_migration"] is True
        assert "Some release body." in result["body"]

    def test_no_frontmatter(self):
        text = "Just a plain release note.\nNo frontmatter here."
        result = updater._parse_release_notes(text)
        assert result["version"] == ""
        assert result["breaking"] is False
        assert result["body"] == text

    def test_breaking_false_default(self):
        text = "---\nversion: 1.0.0\n---\nBody.\n"
        result = updater._parse_release_notes(text)
        assert result["breaking"] is False
        assert result["requires_migration"] is False

    def test_migration_numbered_steps(self):
        text = (
            "---\nversion: 0.3.0\nrequires_migration: true\n---\n"
            "## Migration\n"
            "1. `python migrate.py`\n"
            "2. `pip install -r requirements.txt`\n"
        )
        result = updater._parse_release_notes(text)
        assert result["migration_steps"] == [
            "python migrate.py",
            "pip install -r requirements.txt",
        ]

    def test_migration_bullet_points(self):
        text = (
            "---\nversion: 0.3.0\n---\n"
            "## Migration\n"
            "- `alembic upgrade head`\n"
            "* `echo done`\n"
        )
        result = updater._parse_release_notes(text)
        assert result["migration_steps"] == ["alembic upgrade head", "echo done"]

    def test_migration_run_prefix(self):
        text = (
            "---\nversion: 0.4.0\n---\n"
            "## Migration\n"
            "1. Run: `python setup.py`\n"
        )
        result = updater._parse_release_notes(text)
        assert result["migration_steps"] == ["python setup.py"]

    def test_no_migration_section(self):
        text = "---\nversion: 0.5.0\n---\nNo migration needed.\n"
        result = updater._parse_release_notes(text)
        assert result["migration_steps"] == []


# ── _git ──


def _async_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Build an AsyncMock standing in for ``asyncio.subprocess.Process``."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


class TestGit:
    """``_git`` spawns subprocesses via :func:`asyncio.create_subprocess_exec`
    (v0.20.6 converted it from synchronous ``subprocess.run``). Tests must
    be async and patch the asyncio entry point."""

    @pytest.mark.asyncio
    async def test_calls_subprocess_with_correct_args(self, tmp_path):
        proc = _async_proc()
        with patch("updater.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
            await updater._git("status")
            mock_exec.assert_awaited_once()
            # First positional arg is the executable, followed by the git args.
            args = mock_exec.await_args.args
            assert args[0] == "git"
            assert args[1] == "status"
            assert mock_exec.await_args.kwargs.get("cwd") == str(tmp_path)

    @pytest.mark.asyncio
    async def test_check_false(self):
        proc = _async_proc(returncode=1)
        with patch("updater.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await updater._git("stash", check=False)
        assert result.returncode == 1


# ── fetch_remote_tags ──


class TestFetchRemoteTags:
    @pytest.mark.asyncio
    async def test_fetches_and_returns_tags(self):
        ls_remote_out = (
            "abc123\trefs/tags/v0.2.0\n"
            "def456\trefs/tags/v0.1.0\n"
            "ghi789\trefs/tags/v0.3.0\n"
        )

        async def fake_git(*args, check=True):
            if "ls-remote" in args:
                return subprocess.CompletedProcess(
                    ["git", *args], 0, ls_remote_out, "",
                )
            return subprocess.CompletedProcess(["git", *args], 0, "", "")

        with patch("updater._git", side_effect=fake_git):
            tags = await updater.fetch_remote_tags()
        assert tags == ["v0.1.0", "v0.2.0", "v0.3.0"]


# ── _get_latest_remote_version ──


class TestGetLatestRemoteVersion:
    def test_empty_tags(self):
        assert updater._get_latest_remote_version([]) is None

    def test_returns_last_tag_stripped(self):
        result = updater._get_latest_remote_version(["v0.1.0", "v0.2.0", "v1.0.0"])
        assert result == "1.0.0"


# ── _get_release_notes_for ──


class TestGetReleaseNotesFor:
    @pytest.mark.asyncio
    async def test_tag_found(self):
        notes_text = "---\nversion: 0.2.0\nbreaking: false\n---\nNew stuff.\n"

        async def fake_git(*args, check=True):
            if "show" in args:
                return subprocess.CompletedProcess(["git", *args], 0, notes_text, "")
            return subprocess.CompletedProcess(["git", *args], 0, "", "")

        with patch("updater._git", side_effect=fake_git):
            result = await updater._get_release_notes_for("0.2.0", ["v0.1.0", "v0.2.0"])
        assert result is not None
        assert result["version"] == "0.2.0"

    @pytest.mark.asyncio
    async def test_tag_not_in_list(self):
        assert await updater._get_release_notes_for("0.9.0", ["v0.1.0"]) is None

    @pytest.mark.asyncio
    async def test_git_show_fails(self):
        async def fake_git(*args, check=True):
            return subprocess.CompletedProcess(["git", *args], 1, "", "fatal")

        with patch("updater._git", side_effect=fake_git):
            result = await updater._get_release_notes_for("0.2.0", ["v0.2.0"])
        assert result is None


# ── check_for_updates ──


class TestCheckForUpdates:
    """``check_for_updates`` is async (since v0.20.6) and calls
    :func:`fetch_remote_tags` / :func:`_get_release_notes_for` which are
    also async. Patches must use :class:`AsyncMock`, and the coroutine
    must be awaited."""

    @pytest.mark.asyncio
    async def test_no_new_version(self):
        with patch("updater._save_state"), \
             patch("updater.fetch_remote_tags", new=AsyncMock(return_value=["v0.1.0"])):
            assert await updater.check_for_updates() is None

    @pytest.mark.asyncio
    async def test_already_notified(self, tmp_path):
        _write_state(tmp_path, {
            "notified_versions": ["0.2.0"],
            "last_check": None,
            "last_update": None,
            "update_history": [],
        })
        with patch("updater._save_state"), \
             patch("updater.fetch_remote_tags", new=AsyncMock(return_value=["v0.1.0", "v0.2.0"])):
            assert await updater.check_for_updates() is None

    @pytest.mark.asyncio
    async def test_new_version_available(self):
        with patch("updater._save_state"), \
             patch("updater._get_release_notes_for", new=AsyncMock(return_value=None)), \
             patch("updater.fetch_remote_tags", new=AsyncMock(return_value=["v0.1.0", "v0.2.0"])):
            result = await updater.check_for_updates()
        assert result is not None
        assert result["version"] == "0.2.0"
        assert result["status"] == "available"

    @pytest.mark.asyncio
    async def test_breaking_update(self):
        notes = {
            "version": "0.2.0", "min_compatible": "0.0.0",
            "breaking": True, "requires_migration": False,
            "body": "", "migration_steps": [],
        }
        with patch("updater._save_state"), \
             patch("updater._get_release_notes_for", new=AsyncMock(return_value=notes)), \
             patch("updater.fetch_remote_tags", new=AsyncMock(return_value=["v0.1.0", "v0.2.0"])):
            result = await updater.check_for_updates()
        assert result["status"] == "breaking"

    @pytest.mark.asyncio
    async def test_incompatible(self):
        notes = {
            "version": "0.3.0", "min_compatible": "0.2.0",
            "breaking": False, "requires_migration": False,
            "body": "", "migration_steps": [],
        }
        with patch("updater._save_state"), \
             patch("updater._get_release_notes_for", new=AsyncMock(return_value=notes)), \
             patch("updater.fetch_remote_tags", new=AsyncMock(return_value=["v0.1.0", "v0.3.0"])):
            result = await updater.check_for_updates()
        assert result["status"] == "incompatible"

    @pytest.mark.asyncio
    async def test_fetch_tags_fails(self):
        with patch(
            "updater.fetch_remote_tags",
            new=AsyncMock(side_effect=subprocess.CalledProcessError(1, "git")),
        ):
            assert await updater.check_for_updates() is None

    @pytest.mark.asyncio
    async def test_saves_notification_in_state(self):
        save_spy = MagicMock()
        with patch("updater._save_state", save_spy), \
             patch("updater._get_release_notes_for", new=AsyncMock(return_value=None)), \
             patch("updater.fetch_remote_tags", new=AsyncMock(return_value=["v0.1.0", "v0.2.0"])):
            await updater.check_for_updates()
        save_spy.assert_called_once()
        saved = save_spy.call_args[0][0]
        assert "0.2.0" in saved["notified_versions"]
        assert saved["last_check"] is not None


# ── get_pending_update ──


class TestGetPendingUpdate:
    """``get_pending_update`` was rewritten to be async in v0.20.13 — the
    pre-fix version was sync but called async dependencies without awaiting,
    which silently produced coroutines where dicts were expected. The
    tests below also verify the recovered contract."""

    @pytest.mark.asyncio
    async def test_no_new_version(self):
        with patch("updater.fetch_remote_tags", new=AsyncMock(return_value=["v0.1.0"])):
            assert await updater.get_pending_update() is None

    @pytest.mark.asyncio
    async def test_breaking_returns_none(self):
        notes = {
            "version": "0.2.0", "min_compatible": "0.0.0",
            "breaking": True, "requires_migration": False,
            "body": "", "migration_steps": [],
        }
        with patch("updater._get_release_notes_for", new=AsyncMock(return_value=notes)), \
             patch("updater.fetch_remote_tags", new=AsyncMock(return_value=["v0.1.0", "v0.2.0"])):
            assert await updater.get_pending_update() is None

    @pytest.mark.asyncio
    async def test_incompatible_returns_none(self):
        notes = {
            "version": "0.3.0", "min_compatible": "0.2.0",
            "breaking": False, "requires_migration": False,
            "body": "", "migration_steps": [],
        }
        with patch("updater._get_release_notes_for", new=AsyncMock(return_value=notes)), \
             patch("updater.fetch_remote_tags", new=AsyncMock(return_value=["v0.1.0", "v0.3.0"])):
            assert await updater.get_pending_update() is None

    @pytest.mark.asyncio
    async def test_valid_pending_update(self):
        notes = {
            "version": "0.2.0", "min_compatible": "0.0.0",
            "breaking": False, "requires_migration": False,
            "body": "Improvements.", "migration_steps": [],
        }
        with patch("updater._get_release_notes_for", new=AsyncMock(return_value=notes)), \
             patch("updater.fetch_remote_tags", new=AsyncMock(return_value=["v0.1.0", "v0.2.0"])):
            result = await updater.get_pending_update()
        assert result is not None
        assert result["version"] == "0.2.0"

    @pytest.mark.asyncio
    async def test_fetch_fails(self):
        with patch(
            "updater.fetch_remote_tags",
            new=AsyncMock(side_effect=subprocess.CalledProcessError(1, "git")),
        ):
            assert await updater.get_pending_update() is None


# ── apply_update ──


def _make_git_side_effect(
    pull_ok=True,
    has_stash=True,
    pre_pull_sha="OLDSHA",
    diff_files=None,
    head_detached=False,
    checkout_main_ok=True,
    current_branch="main",
):
    """Build a fake ``_git`` callable for ``apply_update`` integration tests.

    ``pre_pull_sha`` is what ``git rev-parse HEAD`` returns BEFORE the pull
    (the updater captures it for the diff-driven session invalidation).
    ``diff_files`` is the list of repo-relative paths that
    ``git diff --name-only <pre_pull_sha> HEAD`` should report. ``None``
    (default) means "no diff" — the invalidation step becomes a no-op,
    matching the behaviour of every existing test.
    """
    def side_effect(*args, check=True):
        cmd = args[0] if args else ""
        if cmd == "stash" and len(args) > 1 and args[1] == "--include-untracked":
            stdout = "Saved working directory" if has_stash else "No local changes to save"
            return subprocess.CompletedProcess(["git", *args], 0, stdout, "")
        if cmd == "rev-parse" and len(args) > 1 and args[1] == "HEAD":
            return subprocess.CompletedProcess(["git", *args], 0, pre_pull_sha + "\n", "")
        if cmd == "diff" and "--name-only" in args:
            stdout = "\n".join(diff_files or []) + ("\n" if diff_files else "")
            return subprocess.CompletedProcess(["git", *args], 0, stdout, "")
        if cmd == "symbolic-ref":
            if head_detached:
                return subprocess.CompletedProcess(
                    ["git", *args], 1, "",
                    "fatal: ref HEAD is not a symbolic ref",
                )
            return subprocess.CompletedProcess(
                ["git", *args], 0, current_branch + "\n", "",
            )
        if cmd == "checkout" and len(args) > 1 and args[1] == "main":
            rc = 0 if checkout_main_ok else 1
            stderr = "" if checkout_main_ok else "error: pathspec 'main' did not match"
            return subprocess.CompletedProcess(["git", *args], rc, "", stderr)
        if cmd == "pull":
            rc = 0 if pull_ok else 1
            stderr = "" if pull_ok else "fatal: Not possible to fast-forward"
            return subprocess.CompletedProcess(["git", *args], rc, "", stderr)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")
    return side_effect


class TestApplyUpdate:
    pytestmark = pytest.mark.usefixtures("stub_safety_helpers")

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_successful_update(self, mock_git, mock_exec, tmp_path):
        mock_git.side_effect = _make_git_side_effect()
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        success, msg = await updater.apply_update("0.2.0")
        assert success is True
        assert msg == "0.2.0"
        state = _read_state(tmp_path)
        assert state["update_history"][-1]["status"] == "ok"

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_pull_fails(self, mock_git, mock_exec):
        mock_git.side_effect = _make_git_side_effect(pull_ok=False)
        success, msg = await updater.apply_update("0.2.0")
        assert success is False
        assert "git pull --ff-only failed" in msg

    @pytest.mark.asyncio
    @patch("updater._git")
    async def test_preflight_refuses_when_unmerged_paths_exist(self, mock_git):
        """Regression for the 2026-04-21 field incident: a prior failed
        stash pop left the index in a merge-conflict state with no
        ``MERGE_HEAD``. Every subsequent update would then fail on
        ``git pull`` and roll back, leaving the user stuck in a loop.
        The pre-flight gate must catch this up front and refuse to run
        until the operator resolves it.
        """
        def side_effect(*args, check=True):
            if args[:2] == ("ls-files", "--unmerged"):
                # Git prints three stage lines per conflicted path
                # (mode sha stage<TAB>path). We only need one sample line
                # — the helper dedups by path.
                return subprocess.CompletedProcess(
                    ["git", *args], 0,
                    "100644 abc123 1\tbot/continuous.py\n"
                    "100644 def456 2\tbot/continuous.py\n"
                    "100644 789abc 3\tbot/continuous.py\n",
                    "",
                )
            return subprocess.CompletedProcess(["git", *args], 0, "", "")

        mock_git.side_effect = side_effect
        success, msg = await updater.apply_update("0.2.0")
        assert success is False
        assert "Pre-flight check failed" in msg
        assert "unmerged path" in msg
        assert "bot/continuous.py" in msg
        # Pre-flight must not have started stashing / pulling / installing.
        non_preflight = [
            c for c in mock_git.call_args_list
            if c[0][:1] in (("pull",), ("stash",))
            or (len(c[0]) > 1 and c[0][:2] == ("stash", "--include-untracked"))
        ]
        assert non_preflight == [], (
            "pre-flight must abort before stashing or pulling"
        )

    @pytest.mark.asyncio
    @patch("updater._git")
    async def test_preflight_refuses_mid_rebase(self, mock_git, tmp_path, monkeypatch):
        """An in-progress rebase (``.git/rebase-merge/`` present) is
        another unrecoverable-without-intervention state. Pre-flight
        must refuse rather than let the update trample it."""
        mock_git.side_effect = _make_git_side_effect()

        fake_git = tmp_path / ".git"
        (fake_git / "rebase-merge").mkdir(parents=True)
        monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)

        success, msg = await updater.apply_update("0.2.0")
        assert success is False
        assert "Pre-flight check failed" in msg
        assert "rebase" in msg

    @pytest.mark.asyncio
    @patch("updater._git")
    async def test_safe_stash_pop_surfaces_unmerged_paths(self, mock_git, caplog):
        """When ``git stash pop`` leaves conflict markers (the typical
        cause of the field incident), ``_safe_stash_pop`` must log at
        ERROR with the file list, not at a low-signal WARNING. Low-signal
        was what hid the incident in the first place."""
        import logging as _logging
        caplog.set_level(_logging.DEBUG, logger="robyx.updater")

        def side_effect(*args, check=True):
            if args[:2] == ("stash", "pop"):
                return subprocess.CompletedProcess(
                    ["git", *args], 1, "",
                    "CONFLICT (content): Merge conflict in bot/foo.py\n",
                )
            if args[:2] == ("ls-files", "--unmerged"):
                return subprocess.CompletedProcess(
                    ["git", *args], 0,
                    "100644 aaa 1\tbot/foo.py\n"
                    "100644 bbb 2\tbot/foo.py\n"
                    "100644 ccc 3\tbot/foo.py\n",
                    "",
                )
            return subprocess.CompletedProcess(["git", *args], 0, "", "")

        mock_git.side_effect = side_effect
        await updater._safe_stash_pop()

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors, "conflicted stash pop must log at ERROR"
        msg = errors[-1].getMessage()
        assert "UNMERGED" in msg
        assert "bot/foo.py" in msg

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_detached_head_reattaches_to_main(self, mock_git, mock_exec, tmp_path):
        """Regression for v0.20.20: a prior rollback leaves HEAD detached
        at the old tag; the next update must re-attach to main before
        ``git pull --ff-only`` (which aborts on detached HEAD).
        """
        mock_git.side_effect = _make_git_side_effect(head_detached=True)
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        success, _ = await updater.apply_update("0.2.0")
        assert success is True
        checkout_calls = [c for c in mock_git.call_args_list
                          if len(c[0]) > 1 and c[0][0] == "checkout" and c[0][1] == "main"]
        assert len(checkout_calls) == 1

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_detached_head_checkout_main_fails(self, mock_git, mock_exec):
        mock_git.side_effect = _make_git_side_effect(
            head_detached=True, checkout_main_ok=False,
        )
        success, msg = await updater.apply_update("0.2.0")
        assert success is False
        assert "could not switch to main" in msg

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_feature_branch_switches_to_main(self, mock_git, mock_exec, tmp_path):
        """If the operator checked out a feature branch manually, the
        update must still target ``main`` — not whatever HEAD is on.
        """
        mock_git.side_effect = _make_git_side_effect(current_branch="experimental")
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        success, _ = await updater.apply_update("0.2.0")
        assert success is True
        checkout_main = [c for c in mock_git.call_args_list
                         if len(c[0]) > 1 and c[0][0] == "checkout" and c[0][1] == "main"]
        assert len(checkout_main) == 1

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_already_on_main_no_extra_checkout(self, mock_git, mock_exec, tmp_path):
        """The normal path: already on main → no redundant checkout."""
        mock_git.side_effect = _make_git_side_effect(current_branch="main")
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        success, _ = await updater.apply_update("0.2.0")
        assert success is True
        pre_pull_checkouts = [
            c for c in mock_git.call_args_list
            if len(c[0]) > 1 and c[0][0] == "checkout" and c[0][1] == "main"
        ]
        # Zero pre-pull checkout-main calls (rollback path never reached on success).
        assert len(pre_pull_checkouts) == 0

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_migration_step_fails(self, mock_git, mock_exec, tmp_path):
        mock_git.side_effect = _make_git_side_effect()
        (tmp_path / "releases" / "0.2.0.md").write_text(
            "---\nversion: 0.2.0\nrequires_migration: true\n---\n"
            "## Migration\n1. `python migrate.py`\n"
        )
        migration_proc = AsyncMock()
        migration_proc.communicate = AsyncMock(return_value=(b"", b"migration error"))
        migration_proc.returncode = 1
        mock_exec.return_value = migration_proc

        success, msg = await updater.apply_update("0.2.0")
        assert success is False
        assert "Migration step failed" in msg

    @pytest.mark.asyncio
    @patch("updater.asyncio.wait_for", side_effect=[asyncio.TimeoutError()])
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_migration_timeout(self, mock_git, mock_exec, mock_wait_for, tmp_path):
        mock_git.side_effect = _make_git_side_effect()
        (tmp_path / "releases" / "0.2.0.md").write_text(
            "---\nversion: 0.2.0\nrequires_migration: true\n---\n"
            "## Migration\n1. `python slow.py`\n"
        )
        mock_exec.return_value = AsyncMock()
        success, msg = await updater.apply_update("0.2.0")
        assert success is False
        assert "timed out" in msg

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_no_stash_needed(self, mock_git, mock_exec, tmp_path):
        mock_git.side_effect = _make_git_side_effect(has_stash=False)
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        success, _ = await updater.apply_update("0.2.0")
        assert success is True
        pop_calls = [c for c in mock_git.call_args_list
                     if len(c[0]) > 1 and c[0][0] == "stash" and c[0][1] == "pop"]
        assert len(pop_calls) == 0

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_notify_fn_callback(self, mock_git, mock_exec):
        mock_git.side_effect = _make_git_side_effect()
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        notify_fn = AsyncMock()
        success, _ = await updater.apply_update("0.2.0", notify_fn=notify_fn)
        assert success is True
        assert notify_fn.await_count >= 2

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_pip_nonzero_rolls_back(self, mock_git, mock_exec, tmp_path):
        """A non-zero pip install return code must fail the update and
        roll back to the previous version tag. This is the bug that made
        v0.12.0 boot against a venv without Pillow — a silently-failed
        pip install used to be reported as success."""
        mock_git.side_effect = _make_git_side_effect()
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(
            return_value=(b"", b"ERROR: could not find a version that satisfies requirement dummy"),
        )
        pip_proc.returncode = 1
        mock_exec.return_value = pip_proc

        success, msg = await updater.apply_update("0.2.0")

        assert success is False
        assert "pip install returned 1" in msg
        assert "could not find a version" in msg
        # Rollback must reset main to the previous tag (v0.20.22+: no
        # more detached-HEAD `git checkout v<tag>`).
        reset_calls = [
            c for c in mock_git.call_args_list
            if c[0][:3] == ("reset", "--hard", "v0.1.0")
        ]
        assert len(reset_calls) >= 1

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_pip_timeout_rolls_back(self, mock_git, mock_exec, tmp_path):
        mock_git.side_effect = _make_git_side_effect()
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock()  # never actually called
        pip_proc.kill = MagicMock()
        mock_exec.return_value = pip_proc

        # Patch wait_for to raise TimeoutError only for the pip invocation.
        # The fixture does not set requires_migration so no other wait_for
        # call happens in this test.
        with patch("updater.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            success, msg = await updater.apply_update("0.2.0")

        assert success is False
        assert "timed out" in msg
        pip_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    @patch("updater._git")
    async def test_missing_pip_binary_fails_cleanly(self, mock_git, tmp_path):
        mock_git.side_effect = _make_git_side_effect()
        # Remove the fake pip binary that the fixture created.
        (tmp_path / ".venv" / "bin" / "pip").unlink()

        success, msg = await updater.apply_update("0.2.0")
        assert success is False
        assert "venv pip not found" in msg

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_successful_pip_refreshes_bootstrap_marker(self, mock_git, mock_exec, tmp_path):
        mock_git.side_effect = _make_git_side_effect()
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"Successfully installed dummy-1.0", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        success, _ = await updater.apply_update("0.2.0")
        assert success is True

        marker = tmp_path / ".venv" / ".robyx_deps_hash"
        assert marker.exists()
        import hashlib
        expected = hashlib.sha1((tmp_path / "bot" / "requirements.txt").read_bytes()).hexdigest()
        assert marker.read_text().strip() == expected

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_catastrophic_exception(self, mock_git, mock_exec, tmp_path):
        def side_effect(*args, check=True):
            cmd = args[0] if args else ""
            if cmd == "stash" and len(args) > 1 and args[1] == "--include-untracked":
                return subprocess.CompletedProcess(["git"], 0, "Saved working directory", "")
            if cmd == "pull":
                raise RuntimeError("Unexpected catastrophic error")
            return subprocess.CompletedProcess(["git"], 0, "", "")

        mock_git.side_effect = side_effect
        success, msg = await updater.apply_update("0.2.0")
        assert success is False
        assert "Unexpected" in msg or "catastrophic" in msg.lower()
        state = _read_state(tmp_path)
        assert state["update_history"][-1]["status"] == "failed"


# ── migrate_personal_data_to_data_dir (v0.16) ──


class TestMigratePersonalDataToDataDir:
    pytestmark = pytest.mark.usefixtures("stub_safety_helpers")

    """v0.16 pre-pull migration: ``migrate_personal_data_to_data_dir``
    copies tracked runtime files (``tasks.md``, ``specialists.md``,
    ``agents/*.md``, ``specialists/*.md``) to ``data/`` before the git
    pull removes them from the working tree. Must be idempotent —
    re-running is common because the pre-pull hook fires on every
    ``apply_update`` call."""

    def test_noop_when_no_source_files(self, tmp_path):
        # Fresh clone: nothing at repo root to migrate.
        moved = updater.migrate_personal_data_to_data_dir()
        assert moved == []

    def test_copies_tasks_md(self, tmp_path):
        (tmp_path / "tasks.md").write_text("| Task |\n")
        moved = updater.migrate_personal_data_to_data_dir()
        assert "tasks.md" in moved
        assert (tmp_path / "data" / "tasks.md").read_text() == "| Task |\n"
        # Source must still exist — the pull removes it, not the migration.
        assert (tmp_path / "tasks.md").exists()

    def test_copies_specialists_md(self, tmp_path):
        (tmp_path / "specialists.md").write_text("| Agent |\n")
        moved = updater.migrate_personal_data_to_data_dir()
        assert "specialists.md" in moved
        assert (tmp_path / "data" / "specialists.md").read_text() == "| Agent |\n"

    def test_does_not_overwrite_existing_destination(self, tmp_path):
        """Idempotency: a re-run must never clobber data/tasks.md."""
        (tmp_path / "tasks.md").write_text("stale\n")
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "tasks.md").write_text("fresh\n")
        moved = updater.migrate_personal_data_to_data_dir()
        assert "tasks.md" not in moved
        assert (tmp_path / "data" / "tasks.md").read_text() == "fresh\n"

    def test_copies_agent_briefs(self, tmp_path):
        (tmp_path / "agents").mkdir(exist_ok=True)
        (tmp_path / "agents" / "foo.md").write_text("# Foo\n")
        (tmp_path / "agents" / "bar.md").write_text("# Bar\n")
        moved = updater.migrate_personal_data_to_data_dir()
        assert "agents/foo.md" in moved
        assert "agents/bar.md" in moved
        assert (tmp_path / "data" / "agents" / "foo.md").read_text() == "# Foo\n"
        assert (tmp_path / "data" / "agents" / "bar.md").read_text() == "# Bar\n"

    def test_copies_specialist_briefs(self, tmp_path):
        (tmp_path / "specialists").mkdir(exist_ok=True)
        (tmp_path / "specialists" / "rev.md").write_text("Reviewer\n")
        moved = updater.migrate_personal_data_to_data_dir()
        assert "specialists/rev.md" in moved
        assert (tmp_path / "data" / "specialists" / "rev.md").read_text() == "Reviewer\n"

    def test_skips_agent_brief_that_already_exists(self, tmp_path):
        (tmp_path / "agents").mkdir(exist_ok=True)
        (tmp_path / "agents" / "foo.md").write_text("stale\n")
        (tmp_path / "data" / "agents").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "agents" / "foo.md").write_text("fresh\n")
        moved = updater.migrate_personal_data_to_data_dir()
        assert "agents/foo.md" not in moved
        assert (tmp_path / "data" / "agents" / "foo.md").read_text() == "fresh\n"

    def test_migration_runs_before_git_pull_in_apply_update(self, tmp_path):
        """Order-of-operations guarantee: the pre-pull migration must
        execute before ``git pull`` touches the working tree."""
        call_order: list[str] = []

        (tmp_path / "tasks.md").write_text("| Task |\n")
        (tmp_path / "agents").mkdir(exist_ok=True)
        (tmp_path / "agents" / "foo.md").write_text("# Foo\n")

        original_migrate = updater.migrate_personal_data_to_data_dir

        def spy_migrate():
            call_order.append("migrate")
            return original_migrate()

        def fake_git(*args, check=True):
            cmd = args[0] if args else ""
            if cmd == "stash" and len(args) > 1 and args[1] == "--include-untracked":
                return subprocess.CompletedProcess(["git"], 0, "Saved", "")
            if cmd == "pull":
                call_order.append("pull")
            return subprocess.CompletedProcess(["git"], 0, "", "")

        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0

        async def run():
            with patch.object(updater, "migrate_personal_data_to_data_dir", side_effect=spy_migrate), \
                 patch.object(updater, "_git", side_effect=fake_git), \
                 patch("updater.asyncio.create_subprocess_exec", return_value=pip_proc):
                await updater.apply_update("0.2.0")

        asyncio.get_event_loop().run_until_complete(run()) if False else asyncio.run(run())
        assert call_order.index("migrate") < call_order.index("pull")

    @pytest.mark.asyncio
    async def test_notify_fn_receives_migration_message(self, tmp_path):
        """When files are migrated, ``apply_update`` must report the
        relocation to the notify callback so the user sees it in their
        boot summary."""
        (tmp_path / "tasks.md").write_text("| Task |\n")

        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0

        notify_fn = AsyncMock()
        with patch.object(updater, "_git", side_effect=_make_git_side_effect()), \
             patch("updater.asyncio.create_subprocess_exec", return_value=pip_proc):
            success, _ = await updater.apply_update("0.2.0", notify_fn=notify_fn)
        assert success is True
        migration_msgs = [
            c.args[0] for c in notify_fn.await_args_list
            if c.args and "Migrated" in c.args[0]
        ]
        assert migration_msgs, "Expected a 'Migrated ...' notification"
        assert "tasks.md" in migration_msgs[0]


# ── apply_update — diff-driven session invalidation (v0.15.1) ──


class TestApplyUpdateInvalidatesSessions:
    pytestmark = pytest.mark.usefixtures("stub_safety_helpers")
    """After a successful pull, ``apply_update`` must compute the diff
    between the pre-pull commit and the new HEAD and hand the changed
    paths to :func:`session_lifecycle.invalidate_sessions_for_paths` so
    affected agents start a fresh AI-CLI session on their next turn.
    Without this, prompts/briefs that change in a release would never
    reach agents whose Claude sessions pre-existed the upgrade — that
    was the v0.14 → v0.15 silent regression that v0.15.1 fixes
    structurally. v0.15.2 reworks this further: the reset is routed
    through a real ``AgentManager.reset_sessions`` (passed via the new
    ``manager=`` arg of ``apply_update``) so the in-memory and on-disk
    copies stay in sync — the v0.15.0/v0.15.1 file-mutation path was
    silently clobbered in production by the running bot's next
    ``save_state()`` call."""

    def _make_manager(self, agents):
        """Build a fake AgentManager with the agents we care about.

        Records every ``reset_sessions`` call so the tests can assert
        the manager was actually invoked (instead of asserting on a
        post-hoc state.json read, which would not catch the v0.15.0
        regression we are now fixing)."""
        from dataclasses import dataclass, field

        @dataclass
        class _FakeAgent:
            session_id: str
            session_started: bool = False
            message_count: int = 0
            thread_id: int | None = None
            work_dir: str | None = None
            name: str = ""
            agent_type: str = "workspace"

        @dataclass
        class _FakeManager:
            agents: dict = field(default_factory=dict)
            reset_calls: list = field(default_factory=list)

            def reset_sessions(self, agent_names):
                self.reset_calls.append(agent_names)
                if agent_names is None:
                    target = list(self.agents.keys())
                else:
                    target = [n for n in agent_names if n in self.agents]
                for name in target:
                    a = self.agents[name]
                    a.session_id = "fresh-" + name
                    a.session_started = False
                    a.message_count = 0
                return sorted(target)

        return _FakeManager(agents={
            name: _FakeAgent(**fields) for name, fields in agents.items()
        })

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_global_trigger_resets_all_agents(
        self, mock_git, mock_exec, tmp_path,
    ):
        """A diff that touches bot/config.py invalidates every agent."""
        mock_git.side_effect = _make_git_side_effect(
            diff_files=["bot/config.py"],
        )
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        manager = self._make_manager({
            "robyx": dict(name="robyx", session_id="old-k",
                         session_started=True, message_count=5, thread_id=1),
            "assistant": dict(name="assistant", session_id="old-a",
                              session_started=True, message_count=9,
                              thread_id=903,
                              work_dir="/Users/rpix/Workspace"),
        })

        success, _ = await updater.apply_update("0.2.0", manager=manager)
        assert success is True

        # The manager was asked to do a global reset (None means all).
        assert manager.reset_calls == [None]
        # Both agents got fresh session_ids in memory.
        assert manager.agents["robyx"].session_id == "fresh-robyx"
        assert manager.agents["robyx"].session_started is False
        assert manager.agents["robyx"].message_count == 0
        assert manager.agents["assistant"].session_id == "fresh-assistant"
        # Untouched fields survive verbatim on the in-memory agent.
        assert manager.agents["robyx"].thread_id == 1
        assert manager.agents["assistant"].thread_id == 903
        assert manager.agents["assistant"].work_dir == "/Users/rpix/Workspace"

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_per_agent_brief_only_resets_named_agent(
        self, mock_git, mock_exec, tmp_path,
    ):
        """A diff that only touches agents/assistant.md must reset
        assistant and leave the rest of the fleet alone."""
        mock_git.side_effect = _make_git_side_effect(
            diff_files=["agents/assistant.md"],
        )
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        manager = self._make_manager({
            "robyx": dict(name="robyx", session_id="old-k",
                         session_started=True, message_count=5),
            "assistant": dict(name="assistant", session_id="old-a",
                              session_started=True, message_count=9,
                              thread_id=903),
            "code-reviewer": dict(name="code-reviewer", session_id="old-r",
                                  session_started=True, message_count=2,
                                  agent_type="specialist"),
        })

        success, _ = await updater.apply_update("0.2.0", manager=manager)
        assert success is True

        # Only assistant was the target.
        assert manager.reset_calls == [{"assistant"}]
        # assistant was reset
        assert manager.agents["assistant"].session_id == "fresh-assistant"
        assert manager.agents["assistant"].session_started is False
        assert manager.agents["assistant"].message_count == 0
        assert manager.agents["assistant"].thread_id == 903
        # robyx and code-reviewer survived.
        assert manager.agents["robyx"].session_id == "old-k"
        assert manager.agents["robyx"].session_started is True
        assert manager.agents["robyx"].message_count == 5
        assert manager.agents["code-reviewer"].session_id == "old-r"
        assert manager.agents["code-reviewer"].session_started is True

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_irrelevant_paths_do_not_call_reset(
        self, mock_git, mock_exec, tmp_path,
    ):
        """A diff that only touches Python logic files (not prompts or
        briefs) must NOT invalidate any session — those changes are
        picked up by the process restart that follows apply_update."""
        mock_git.side_effect = _make_git_side_effect(
            diff_files=["bot/handlers.py", "tests/test_handlers.py"],
        )
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        manager = self._make_manager({
            "robyx": dict(name="robyx", session_id="untouched",
                         session_started=True, message_count=5),
        })

        success, _ = await updater.apply_update("0.2.0", manager=manager)
        assert success is True

        # Manager was never asked to reset anything.
        assert manager.reset_calls == []
        assert manager.agents["robyx"].session_id == "untouched"
        assert manager.agents["robyx"].session_started is True
        assert manager.agents["robyx"].message_count == 5

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_no_manager_skips_invalidation(
        self, mock_git, mock_exec, tmp_path,
    ):
        """If apply_update is called without a manager (e.g. legacy
        callers, or a CLI invocation outside the bot process), the
        update must still succeed — invalidation is just skipped."""
        mock_git.side_effect = _make_git_side_effect(
            diff_files=["bot/config.py"],
        )
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        success, msg = await updater.apply_update("0.2.0")  # no manager
        assert success is True
        assert msg == "0.2.0"

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_specialist_brief_resets_only_specialist(
        self, mock_git, mock_exec, tmp_path,
    ):
        mock_git.side_effect = _make_git_side_effect(
            diff_files=["specialists/code-reviewer.md"],
        )
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        manager = self._make_manager({
            "robyx": dict(name="robyx", session_id="old-k",
                         session_started=True, message_count=5),
            "code-reviewer": dict(name="code-reviewer", session_id="old-r",
                                  session_started=True, message_count=2,
                                  agent_type="specialist"),
        })

        success, _ = await updater.apply_update("0.2.0", manager=manager)
        assert success is True

        assert manager.reset_calls == [{"code-reviewer"}]
        assert manager.agents["robyx"].session_id == "old-k"
        assert manager.agents["code-reviewer"].session_id == "fresh-code-reviewer"
        assert manager.agents["code-reviewer"].agent_type == "specialist"

    @pytest.mark.asyncio
    @patch("updater.asyncio.create_subprocess_exec")
    @patch("updater._git")
    async def test_notify_fn_reports_reset_summary(
        self, mock_git, mock_exec, tmp_path,
    ):
        """When agents are reset, the user-facing progress notification
        must mention which ones, so the boot summary on Telegram is
        actionable instead of a silent surprise."""
        mock_git.side_effect = _make_git_side_effect(
            diff_files=["agents/assistant.md"],
        )
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0
        mock_exec.return_value = pip_proc

        manager = self._make_manager({
            "assistant": dict(name="assistant", session_id="old-a",
                              session_started=True, message_count=9),
        })

        notify_fn = AsyncMock()
        success, _ = await updater.apply_update(
            "0.2.0", notify_fn=notify_fn, manager=manager,
        )
        assert success is True

        notify_messages = [c.args[0] for c in notify_fn.await_args_list]
        reset_msgs = [m for m in notify_messages if "Reset AI sessions" in m]
        assert reset_msgs, "expected a notify message about session reset, got %r" % notify_messages
        assert "assistant" in reset_msgs[0]


# ── restart_service ──


class TestRestartService:
    @patch("updater._get_uid", return_value=501)
    @patch("updater.subprocess.Popen")
    @patch("updater.platform.system", return_value="Darwin")
    def test_macos(self, mock_system, mock_popen, mock_uid):
        updater.restart_service()
        mock_popen.assert_called_once_with(
            ["launchctl", "kickstart", "-k", "gui/501/com.robyx.bot"],
            start_new_session=True,
        )

    @patch("updater.subprocess.Popen")
    @patch("updater.platform.system", return_value="Linux")
    def test_linux(self, mock_system, mock_popen):
        updater.restart_service()
        mock_popen.assert_called_once_with(
            ["systemctl", "--user", "restart", "robyx"],
            start_new_session=True,
        )

    @patch("updater.subprocess.Popen")
    @patch("updater.platform.system", return_value="Windows")
    def test_windows(self, mock_system, mock_popen):
        updater.restart_service()
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert "powershell" in args[0]

    @patch("updater.subprocess.Popen")
    @patch("updater.platform.system", return_value="FreeBSD")
    def test_unsupported_platform(self, mock_system, mock_popen):
        updater.restart_service()
        mock_popen.assert_not_called()

    @patch("updater._get_uid", return_value=501)
    @patch("updater.subprocess.Popen", side_effect=OSError("popen failed"))
    @patch("updater.platform.system", return_value="Darwin")
    def test_exception_logged(self, mock_system, mock_popen, mock_uid):
        updater.restart_service()


# ── _get_uid ──


class TestGetUid:
    @patch("os.getuid", return_value=1000)
    def test_returns_uid(self, mock_getuid):
        assert updater._get_uid() == 1000


# ═══════════════════════════════════════════════════════════════════════════
# _parse_release_notes — frontmatter line without colon (covers line 82)
# ═══════════════════════════════════════════════════════════════════════════


class TestParseReleaseNotesEdgeCases:
    def test_frontmatter_line_without_colon(self):
        """A frontmatter line with no colon is silently skipped (line 82: continue)."""
        text = "---\nversion: 1.0.0\nno colon here\n---\nBody.\n"
        result = updater._parse_release_notes(text)
        assert result["version"] == "1.0.0"
        assert "Body." in result["body"]

    def test_multiple_lines_without_colons(self):
        """Multiple colon-less lines are all skipped, valid keys still parsed."""
        text = (
            "---\n"
            "version: 2.0.0\n"
            "just a line\n"
            "another bare line\n"
            "breaking: true\n"
            "---\n"
            "Release body.\n"
        )
        result = updater._parse_release_notes(text)
        assert result["version"] == "2.0.0"
        assert result["breaking"] is True


# ═══════════════════════════════════════════════════════════════════════════
# get_pending_update — _get_latest_remote_version returns None (covers line 179)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetPendingUpdateEdgeCases:
    @pytest.mark.asyncio
    async def test_no_tags_returns_none(self):
        """When fetch returns empty list, _get_latest_remote_version returns None,
        so get_pending_update returns None (no-latest branch)."""
        with patch("updater.fetch_remote_tags", new=AsyncMock(return_value=[])):
            assert await updater.get_pending_update() is None


# ═══════════════════════════════════════════════════════════════════════════
# Snapshot + restore + smoke test (v0.20.14 safety guards)
# ═══════════════════════════════════════════════════════════════════════════


class TestSnapshotDataDir:
    def test_creates_tar_with_data_files_and_returns_path(self, tmp_path):
        """Snapshot must contain every file that was in DATA_DIR at the
        moment of capture, except for the backups/ subdir itself."""
        (updater.DATA_DIR / "state.json").write_text('{"x": 1}')
        nested = updater.DATA_DIR / "agents"
        nested.mkdir(exist_ok=True)
        (nested / "alice.md").write_text("hello")

        snap = updater._snapshot_data_dir("0.20.13", "0.20.14")
        assert snap is not None
        assert snap.exists()
        assert snap.parent.name == "backups"
        assert snap.name.startswith("pre-update-0.20.13-to-0.20.14-")
        assert snap.name.endswith(".tar.gz")

        import tarfile as _tf
        with _tf.open(str(snap)) as tf:
            names = sorted(m.name for m in tf.getmembers())
        assert "./state.json" in names
        assert "./agents/alice.md" in names

    def test_excludes_backups_subdir(self, tmp_path):
        """Without the exclusion, every successive update would tar the
        previous snapshot inside the new one — runaway growth."""
        (updater.DATA_DIR / "state.json").write_text("{}")
        backups = updater.DATA_DIR / "backups"
        backups.mkdir()
        (backups / "old-snapshot.tar.gz").write_bytes(b"\x1f\x8b" + b"\x00" * 40)

        snap = updater._snapshot_data_dir("0.1.0", "0.2.0")
        assert snap is not None

        import tarfile as _tf
        with _tf.open(str(snap)) as tf:
            names = [m.name for m in tf.getmembers()]
        assert not any(n.startswith("./backups") for n in names), (
            "snapshot leaked the backups/ subdir: %s" % names
        )

    def test_returns_none_on_failure(self, tmp_path):
        """Snapshot failure must NOT block the update — caller treats
        ``None`` as "proceed without backup" (logged at WARNING)."""
        with patch("updater.tarfile.open", side_effect=OSError("disk full")):
            snap = updater._snapshot_data_dir("a", "b")
        assert snap is None


class TestPruneOldSnapshots:
    def test_keeps_most_recent_n(self, tmp_path):
        backups = updater.DATA_DIR / "backups"
        backups.mkdir()
        # Create 5 fake snapshot files with strictly increasing mtimes.
        import os as _os
        import time as _time
        names = []
        for i in range(5):
            p = backups / ("pre-update-0.0.0-to-0.0.%d-stamp.tar.gz" % i)
            p.write_bytes(b"")
            ts = _time.time() + i
            _os.utime(p, (ts, ts))
            names.append(p)

        updater._prune_old_snapshots(backups, keep=3)

        survivors = sorted(p.name for p in backups.glob("pre-update-*.tar.gz"))
        # The 2 oldest should have been removed; the 3 newest survive.
        assert len(survivors) == 3
        assert all(n.endswith(("0.0.2-stamp.tar.gz",
                               "0.0.3-stamp.tar.gz",
                               "0.0.4-stamp.tar.gz")) for n in survivors)


class TestRestoreDataDir:
    def test_round_trip_restores_original_contents(self, tmp_path):
        (updater.DATA_DIR / "state.json").write_text('{"v": 1}')
        snap = updater._snapshot_data_dir("a", "b")
        assert snap is not None

        # Mutate after snapshot — simulates a botched migration.
        (updater.DATA_DIR / "state.json").write_text('{"v": "BROKEN"}')
        (updater.DATA_DIR / "leftover.txt").write_text("garbage")

        ok = updater._restore_data_dir(snap)
        assert ok is True
        assert (updater.DATA_DIR / "state.json").read_text() == '{"v": 1}'
        # Restore overlays the snapshot — files added after the snapshot
        # remain (they were never in the tarball). This is desirable: it
        # means a successful migration that adds *new* files isn't undone
        # if a *later* step fails. Migrations that need atomic rollback
        # should write to a staging area first.

    def test_returns_false_for_missing_snapshot(self, tmp_path):
        ok = updater._restore_data_dir(tmp_path / "nope.tar.gz")
        assert ok is False

    def test_rejects_archive_exceeding_size_cap(self, tmp_path, monkeypatch):
        """Pass 2 P2-72: _restore_data_dir must refuse a tarball whose
        cumulative uncompressed member sizes exceed MAX_RESTORE_TOTAL_BYTES
        (zip-bomb / disk-fill guard). Build a legitimate small snapshot,
        then drop the cap below its real size so the guard trips."""
        (updater.DATA_DIR / "state.json").write_text('{"v": 1}')
        snap = updater._snapshot_data_dir("a", "b")
        assert snap is not None

        monkeypatch.setattr(updater, "MAX_RESTORE_TOTAL_BYTES", 0)
        ok = updater._restore_data_dir(snap)
        assert ok is False


class TestScrubbedChildEnv:
    """Pass 2 P2-71: pip and migration subprocesses must not inherit
    platform tokens or AI provider keys from the bot's own env."""

    def test_strips_platform_bot_tokens(self, monkeypatch):
        for key in (
            "ROBYX_BOT_TOKEN",
            "KAELOPS_BOT_TOKEN",
            "DISCORD_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
        ):
            monkeypatch.setenv(key, "secret-" + key)
        env = updater._scrubbed_child_env()
        assert "ROBYX_BOT_TOKEN" not in env
        assert "KAELOPS_BOT_TOKEN" not in env
        assert "DISCORD_BOT_TOKEN" not in env
        assert "SLACK_BOT_TOKEN" not in env
        assert "SLACK_APP_TOKEN" not in env

    def test_strips_ai_provider_keys(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        env = updater._scrubbed_child_env()
        assert "OPENAI_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env

    def test_preserves_unrelated_env(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/robyx")
        monkeypatch.setenv("PIP_INDEX_URL", "https://pypi.org/simple/")
        env = updater._scrubbed_child_env()
        assert env.get("PATH") == "/usr/bin:/bin"
        assert env.get("HOME") == "/home/robyx"
        assert env.get("PIP_INDEX_URL") == "https://pypi.org/simple/"


class TestApplyUpdateChildEnvHygiene:
    """Pass 2 P2-71: verify apply_update actually threads the scrubbed env
    through to pip and to migration-step subprocesses."""

    @pytest.mark.asyncio
    async def test_pip_install_invoked_with_scrubbed_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBYX_BOT_TOKEN", "tg-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
        monkeypatch.setenv("PATH", "/usr/bin")

        (tmp_path / "VERSION").write_text("0.1.0\n")
        (tmp_path / "releases" / "0.2.0.md").write_text(
            "---\nversion: 0.2.0\nmin_compatible: 0.1.0\n---\n\nbody\n"
        )

        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0

        captured = {}

        async def _capture_spawn(*args, **kwargs):
            if args and "pip" in str(args[0]):
                captured["pip_env"] = kwargs.get("env")
            return pip_proc

        with patch("updater._git", side_effect=_make_git_side_effect()), \
             patch("updater.asyncio.create_subprocess_exec", side_effect=_capture_spawn), \
             patch("updater._snapshot_data_dir", return_value=None), \
             patch("updater._post_update_smoke_test", new=AsyncMock(return_value=(True, ""))):
            await updater.apply_update("0.2.0")

        assert "pip_env" in captured, "pip install was not invoked"
        env = captured["pip_env"]
        assert env is not None, "pip must receive an explicit env kwarg"
        assert "ROBYX_BOT_TOKEN" not in env
        assert "OPENAI_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin"

    @pytest.mark.asyncio
    async def test_migration_step_with_quoted_argument_tokenized_via_shlex(
        self, tmp_path,
    ):
        """Regression: migration steps with quoted args must tokenize via
        shlex, not str.split. Plain split would break `mv "old name" new`
        into 4 tokens ('mv', '"old', 'name"', 'new') instead of 3.
        """
        (tmp_path / "VERSION").write_text("0.1.0\n")
        (tmp_path / "releases" / "0.2.0.md").write_text(
            "---\n"
            "version: 0.2.0\n"
            "min_compatible: 0.1.0\n"
            "requires_migration: true\n"
            "---\n\n"
            "## Migration\n\n"
            '1. Run: `python -m pip install "package[extra]==1.2"`\n'
        )

        mig_proc = AsyncMock()
        mig_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mig_proc.returncode = 0

        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0

        captured_argv: list[tuple] = []

        async def _capture_spawn(*args, **kwargs):
            captured_argv.append(args)
            if args and "/.venv/" in str(args[0]):
                return pip_proc
            return mig_proc

        with patch("updater._git", side_effect=_make_git_side_effect()), \
             patch("updater.asyncio.create_subprocess_exec", side_effect=_capture_spawn), \
             patch("updater._snapshot_data_dir", return_value=None), \
             patch("updater._post_update_smoke_test", new=AsyncMock(return_value=(True, ""))):
            await updater.apply_update("0.2.0")

        mig_calls = [a for a in captured_argv if a and a[0] == "python"]
        assert mig_calls, "migration step was not invoked"
        argv = mig_calls[0]
        # shlex strips the quotes; the literal argument is preserved as a
        # single token. str.split would have produced 5 fragments.
        assert argv == ("python", "-m", "pip", "install", "package[extra]==1.2")

    @pytest.mark.asyncio
    async def test_migration_step_with_unbalanced_quotes_fails_clean(
        self, tmp_path,
    ):
        """A malformed step (unbalanced quote) must fail the update with a
        clear error rather than crash the parser deep in subprocess code."""
        (tmp_path / "VERSION").write_text("0.1.0\n")
        (tmp_path / "releases" / "0.2.0.md").write_text(
            "---\n"
            "version: 0.2.0\n"
            "min_compatible: 0.1.0\n"
            "requires_migration: true\n"
            "---\n\n"
            "## Migration\n\n"
            '1. Run: `echo "unclosed`\n'
        )

        with patch("updater._git", side_effect=_make_git_side_effect()), \
             patch("updater._snapshot_data_dir", return_value=None), \
             patch("updater._restore_data_dir"), \
             patch("updater._rollback_code_to", new=AsyncMock()):
            ok, msg = await updater.apply_update("0.2.0")

        assert ok is False
        assert "Unparseable migration step" in msg

    @pytest.mark.asyncio
    async def test_migration_step_invoked_with_scrubbed_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "dc-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")

        (tmp_path / "VERSION").write_text("0.1.0\n")
        (tmp_path / "releases" / "0.2.0.md").write_text(
            "---\n"
            "version: 0.2.0\n"
            "min_compatible: 0.1.0\n"
            "requires_migration: true\n"
            "---\n\n"
            "## Migration\n\n"
            "1. Run: `echo hello`\n"
        )

        mig_proc = AsyncMock()
        mig_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mig_proc.returncode = 0

        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0

        captured_envs = []

        async def _capture_spawn(*args, **kwargs):
            captured_envs.append((args, kwargs.get("env")))
            if args and "pip" in str(args[0]):
                return pip_proc
            return mig_proc

        with patch("updater._git", side_effect=_make_git_side_effect()), \
             patch("updater.asyncio.create_subprocess_exec", side_effect=_capture_spawn), \
             patch("updater._snapshot_data_dir", return_value=None), \
             patch("updater._post_update_smoke_test", new=AsyncMock(return_value=(True, ""))):
            await updater.apply_update("0.2.0")

        mig_calls = [
            env for args, env in captured_envs
            if args and args[0] == "echo"
        ]
        assert mig_calls, "migration step was not invoked"
        env = mig_calls[0]
        assert env is not None
        assert "DISCORD_BOT_TOKEN" not in env
        assert "ANTHROPIC_API_KEY" not in env


class TestPostUpdateSmokeTest:
    @pytest.mark.asyncio
    async def test_returns_false_when_venv_python_missing(self, tmp_path):
        # Default fixture creates only a fake pip; remove the python so we
        # exercise the missing-binary branch.
        py = updater.PROJECT_ROOT / ".venv" / "bin" / "python"
        if py.exists():
            py.unlink()
        ok, err = await updater._post_update_smoke_test()
        assert ok is False
        assert "venv python not found" in err

    @pytest.mark.asyncio
    async def test_success_returns_true(self, tmp_path):
        (updater.PROJECT_ROOT / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        with patch("updater.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            ok, err = await updater._post_update_smoke_test()
        assert ok is True
        assert err == ""

    @pytest.mark.asyncio
    async def test_invokes_bot_py_with_smoke_test_flag(self, tmp_path):
        """Regression for v0.20.20: the old ``python -c "import bot.bot"``
        form fails on any real install because ``bot/`` is not on
        ``sys.path`` and ``bot/bot.py`` does ``import _bootstrap``. The
        fix runs ``bot/bot.py --smoke-test`` directly, mirroring the
        production service invocation.
        """
        (updater.PROJECT_ROOT / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        spawn = AsyncMock(return_value=proc)
        with patch("updater.asyncio.create_subprocess_exec", spawn):
            await updater._post_update_smoke_test()
        args = spawn.call_args.args
        assert args[1].endswith("bot/bot.py") or args[1].endswith("bot\\bot.py")
        assert "--smoke-test" in args

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_failure_with_tail(self, tmp_path):
        (updater.PROJECT_ROOT / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 1\n")
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"ImportError: No module 'foo'"))
        proc.returncode = 1
        with patch("updater.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            ok, err = await updater._post_update_smoke_test()
        assert ok is False
        assert "ImportError" in err

    @pytest.mark.asyncio
    async def test_timeout_kills_proc_and_returns_failure(self, tmp_path):
        (updater.PROJECT_ROOT / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch("updater.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            ok, err = await updater._post_update_smoke_test()
        assert ok is False
        assert "timed out" in err
        proc.kill.assert_called_once()


class TestApplyUpdateSafetyIntegration:
    """End-to-end: failed smoke test must roll back code AND restore data/."""

    @pytest.mark.asyncio
    async def test_failed_smoke_test_restores_snapshot_and_rolls_back(self, tmp_path):
        # Make the snapshot helper actually write a real tar — we override
        # the autouse stub so we can assert on the restore path.
        (updater.DATA_DIR / "state.json").write_text('{"original": true}')

        from tests.test_updater import _make_git_side_effect  # local import to reuse
        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(b"", b""))
        pip_proc.returncode = 0

        with patch("updater._git", side_effect=_make_git_side_effect()), \
             patch("updater.asyncio.create_subprocess_exec", AsyncMock(return_value=pip_proc)), \
             patch("updater._snapshot_data_dir", wraps=updater._snapshot_data_dir), \
             patch(
                 "updater._post_update_smoke_test",
                 new=AsyncMock(return_value=(False, "ImportError: broken")),
             ), \
             patch("updater._restore_data_dir", wraps=updater._restore_data_dir) as restore_spy:
            success, msg = await updater.apply_update("0.2.0")

        assert success is False
        assert "Smoke test failed" in msg
        assert "ImportError: broken" in msg
        # The restore_data_dir spy must have been called with the snapshot
        # path created during this update.
        restore_spy.assert_called_once()
