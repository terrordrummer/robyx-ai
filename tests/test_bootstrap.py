"""Tests for bot/_bootstrap.py — startup dep check."""

import hashlib
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fresh_bootstrap(tmp_path, monkeypatch):
    """Reload bot/_bootstrap.py pointing at a tmp venv + requirements file.

    The module executes ``ensure_dependencies()`` at import time, so we
    must reload it after pointing the module-level paths at our fixtures.
    Also clears ``PYTEST_CURRENT_TEST`` so the early-return guard does
    not short-circuit the code under test.
    """
    venv = tmp_path / ".venv"
    bin_dir = "Scripts" if sys.platform == "win32" else "bin"
    pip_name = "pip.exe" if sys.platform == "win32" else "pip"
    (venv / bin_dir).mkdir(parents=True)
    fake_pip = venv / bin_dir / pip_name
    fake_pip.write_text("#!/bin/sh\nexit 0\n")
    fake_pip.chmod(0o755)

    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    req = bot_dir / "requirements.txt"
    req.write_text("dummy==1.0\n")

    # Remove the active PYTEST marker so the guard doesn't skip the code.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    # Import and rewire paths
    if "_bootstrap" in sys.modules:
        del sys.modules["_bootstrap"]
    import _bootstrap as bs
    monkeypatch.setattr(bs, "_BOT_DIR", bot_dir)
    monkeypatch.setattr(bs, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bs, "_REQUIREMENTS", req)
    monkeypatch.setattr(bs, "_VENV_DIR", venv)
    return bs, tmp_path, req, venv


class TestEnsureDependencies:
    def test_first_run_installs_and_writes_marker(self, fresh_bootstrap):
        bs, root, req, venv = fresh_bootstrap
        expected_hash = hashlib.sha1(req.read_bytes()).hexdigest()

        with patch("_bootstrap.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            bs.ensure_dependencies()

        mock_run.assert_called_once()
        marker = venv / ".robyx_deps_hash"
        assert marker.exists()
        assert marker.read_text().strip() == expected_hash

    def test_second_run_same_requirements_is_noop(self, fresh_bootstrap):
        bs, root, req, venv = fresh_bootstrap
        marker = venv / ".robyx_deps_hash"
        marker.write_text(hashlib.sha1(req.read_bytes()).hexdigest())

        with patch("_bootstrap.subprocess.run") as mock_run:
            bs.ensure_dependencies()
        mock_run.assert_not_called()

    def test_hash_mismatch_triggers_reinstall(self, fresh_bootstrap):
        bs, root, req, venv = fresh_bootstrap
        marker = venv / ".robyx_deps_hash"
        marker.write_text("stale-hash-value")

        with patch("_bootstrap.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            bs.ensure_dependencies()
        mock_run.assert_called_once()
        assert marker.read_text().strip() == hashlib.sha1(req.read_bytes()).hexdigest()

    def test_pip_failure_does_not_update_marker(self, fresh_bootstrap):
        bs, root, req, venv = fresh_bootstrap

        with patch("_bootstrap.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
            bs.ensure_dependencies()

        marker = venv / ".robyx_deps_hash"
        assert not marker.exists()

    def test_pip_subprocess_gets_scrubbed_env(self, fresh_bootstrap, monkeypatch):
        """P2-86: pip must run with a scrubbed environment — platform
        tokens and AI provider keys MUST NOT leak into the pip
        subprocess, mirroring P2-71 on bot/updater.py. A malicious
        setup.py in a transitive dep or a PIP_INDEX_URL-redirected
        proxy would otherwise see our secrets."""
        bs, root, req, venv = fresh_bootstrap
        monkeypatch.setenv("ROBYX_BOT_TOKEN", "telegram-secret")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "slack-secret")
        monkeypatch.setenv("SLACK_APP_TOKEN", "slack-app-secret")
        monkeypatch.setenv("KAELOPS_BOT_TOKEN", "legacy-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        # Operator-set env that MUST pass through unchanged.
        monkeypatch.setenv("PIP_INDEX_URL", "https://proxy.example/simple")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
        monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")

        with patch("_bootstrap.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            bs.ensure_dependencies()

        mock_run.assert_called_once()
        env = mock_run.call_args.kwargs.get("env")
        assert env is not None, "env= must be explicitly passed to pip"
        # Secrets scrubbed.
        for key in (
            "ROBYX_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN", "KAELOPS_BOT_TOKEN", "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        ):
            assert key not in env, (
                "secret %s must be scrubbed from pip env" % key
            )
        # Operator env preserved.
        assert env.get("PIP_INDEX_URL") == "https://proxy.example/simple"
        assert env.get("HTTPS_PROXY") == "http://proxy.example:8080"
        assert "PATH" in env

    def test_scrubbed_env_returns_dict_without_secrets(self, fresh_bootstrap, monkeypatch):
        """Unit test for the helper itself — documentation of the exact
        scrub list, independent of the subprocess wiring."""
        bs, _root, _req, _venv = fresh_bootstrap
        monkeypatch.setenv("ROBYX_BOT_TOKEN", "x")
        monkeypatch.setenv("SAFE_VAR", "keep-me")

        env = bs._scrubbed_child_env()
        assert "ROBYX_BOT_TOKEN" not in env
        assert env.get("SAFE_VAR") == "keep-me"
        # Scrub list must match updater.py for consistency.
        assert bs._CHILD_ENV_SCRUB == frozenset({
            "ROBYX_BOT_TOKEN",
            "KAELOPS_BOT_TOKEN",
            "DISCORD_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        })

    def test_timeout_does_not_crash(self, fresh_bootstrap):
        import subprocess as _sp
        bs, root, req, venv = fresh_bootstrap

        with patch(
            "_bootstrap.subprocess.run",
            side_effect=_sp.TimeoutExpired(cmd="pip", timeout=600),
        ):
            bs.ensure_dependencies()  # must not raise

        marker = venv / ".robyx_deps_hash"
        assert not marker.exists()

    def test_missing_requirements_is_noop(self, fresh_bootstrap):
        bs, root, req, venv = fresh_bootstrap
        req.unlink()

        with patch("_bootstrap.subprocess.run") as mock_run:
            bs.ensure_dependencies()
        mock_run.assert_not_called()

    def test_missing_venv_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        bot_dir = tmp_path / "bot"
        bot_dir.mkdir()
        (bot_dir / "requirements.txt").write_text("dummy==1.0\n")

        if "_bootstrap" in sys.modules:
            del sys.modules["_bootstrap"]
        import _bootstrap as bs
        monkeypatch.setattr(bs, "_BOT_DIR", bot_dir)
        monkeypatch.setattr(bs, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(bs, "_REQUIREMENTS", bot_dir / "requirements.txt")
        monkeypatch.setattr(bs, "_VENV_DIR", tmp_path / ".venv")  # does not exist

        with patch("_bootstrap.subprocess.run") as mock_run:
            bs.ensure_dependencies()
        mock_run.assert_not_called()


class TestMigratePersonalDataIfNeeded:
    """v0.16 boot-time safety net: ``migrate_personal_data_if_needed``
    covers the path where the user manually runs ``git pull`` and then
    restarts the bot without going through ``apply_update``. It mirrors
    the pre-pull migration in ``bot/updater.py`` but runs at boot, before
    any bot-local imports."""

    def test_noop_when_nothing_to_migrate(self, fresh_bootstrap, monkeypatch):
        bs, root, _req, _venv = fresh_bootstrap
        data_dir = root / "data"
        monkeypatch.setattr(bs, "_DATA_DIR", data_dir)
        moved = bs.migrate_personal_data_if_needed()
        assert moved == []

    def test_copies_repo_root_files(self, fresh_bootstrap, monkeypatch):
        bs, root, _req, _venv = fresh_bootstrap
        data_dir = root / "data"
        monkeypatch.setattr(bs, "_DATA_DIR", data_dir)
        (root / "tasks.md").write_text("| Task |\n")
        (root / "specialists.md").write_text("| Agent |\n")
        (root / "agents").mkdir(exist_ok=True)
        (root / "agents" / "foo.md").write_text("# Foo\n")
        (root / "specialists").mkdir(exist_ok=True)
        (root / "specialists" / "rev.md").write_text("Rev\n")

        moved = bs.migrate_personal_data_if_needed()
        assert set(moved) == {
            "tasks.md",
            "specialists.md",
            "agents/foo.md",
            "specialists/rev.md",
        }
        assert (data_dir / "tasks.md").read_text() == "| Task |\n"
        assert (data_dir / "agents" / "foo.md").read_text() == "# Foo\n"
        assert (data_dir / "specialists" / "rev.md").read_text() == "Rev\n"

    def test_idempotent_does_not_overwrite(self, fresh_bootstrap, monkeypatch):
        """Second run must not clobber an existing data/ copy."""
        bs, root, _req, _venv = fresh_bootstrap
        data_dir = root / "data"
        monkeypatch.setattr(bs, "_DATA_DIR", data_dir)
        (root / "tasks.md").write_text("stale\n")
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "tasks.md").write_text("fresh\n")

        moved = bs.migrate_personal_data_if_needed()
        assert "tasks.md" not in moved
        assert (data_dir / "tasks.md").read_text() == "fresh\n"

    def test_untracked_agent_briefs_are_caught(self, fresh_bootstrap, monkeypatch):
        """The Mac runtime install has untracked manual briefs like
        ``agents/zeus-engine.md`` that git pull leaves behind — the
        bootstrap safety net must scoop those up on the next boot."""
        bs, root, _req, _venv = fresh_bootstrap
        data_dir = root / "data"
        monkeypatch.setattr(bs, "_DATA_DIR", data_dir)
        (root / "agents").mkdir(exist_ok=True)
        (root / "agents" / "zeus-engine.md").write_text("Z\n")

        moved = bs.migrate_personal_data_if_needed()
        assert "agents/zeus-engine.md" in moved
        assert (data_dir / "agents" / "zeus-engine.md").read_text() == "Z\n"
