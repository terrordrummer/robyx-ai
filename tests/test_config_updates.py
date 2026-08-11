import importlib.util
import os
import signal
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config_updates
from config_schema import (
    CHAT_ENV_FIELDS,
    EnvField,
    EnvValidationError,
    EnvValueKind,
    typed_env,
)
from config_updates import (
    ConfigPreflightError,
    KNOWN_ENV_KEYS,
    LocalOnlyConfigAssignmentError,
    apply_env_updates,
    apply_env_updates_transactionally,
    parse_direct_env_updates,
    preflight_candidate_config,
)


def _valid_values(tmp_path: Path) -> dict[str, str]:
    executable = tmp_path / "my-cli"
    executable.write_text("#!/bin/sh\necho ok\n")
    executable.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {
        "OPENAI_API_KEY": "sk-test",
        "AI_BACKEND": "codex",
        "AI_CLI_PATH": str(executable),
        "CLAUDE_PERMISSION_MODE": "bypassPermissions",
        "SCHEDULER_INTERVAL": "120",
        "UPDATE_CHECK_INTERVAL": "3600",
        "REMINDER_MAX_AGE_SECONDS": "604800",
        "ROBYX_PLATFORM": "discord",
        "ROBYX_WORKSPACE": str(workspace),
        "KAELOPS_PLATFORM": "slack",
        "KAELOPS_WORKSPACE": str(workspace),
    }


def test_known_keys_are_derived_from_complete_typed_schema():
    assert KNOWN_ENV_KEYS == frozenset(CHAT_ENV_FIELDS)


def test_parse_direct_env_updates_accepts_every_known_key(tmp_path):
    values = _valid_values(tmp_path)
    updates = parse_direct_env_updates(
        "\n".join("%s=%s" % item for item in values.items()),
    )
    assert updates == values


def test_parse_direct_env_updates_rejects_natural_language():
    assert parse_direct_env_updates("here is the key: sk-test") == {}


def test_participant_security_policy_is_not_chat_editable():
    with pytest.raises(LocalOnlyConfigAssignmentError):
        parse_direct_env_updates("COLLAB_PARTICIPANT_POLICY=disabled")


@pytest.mark.parametrize(
    "key",
    [
        "ROBYX_BOT_TOKEN",
        "KAELOPS_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "DISCORD_BOT_TOKEN",
        "ROBYX_OWNER_ID",
        "COLLAB_PARTICIPANT_POLICY",
        "CODEX_SANDBOX",
        "AI_TIMEOUT",
        "EVENT_RETENTION_DAYS",
        "AWS_SECRET_ACCESS_KEY",
    ],
)
def test_sensitive_or_local_only_assignment_is_intercepted_without_value(key):
    secret = "must-not-reach-ai"
    with pytest.raises(LocalOnlyConfigAssignmentError) as caught:
        parse_direct_env_updates("Please set %s=%s" % (key, secret))
    assert caught.value.key == key
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("key", "invalid"),
    [
        ("OPENAI_API_KEY", "sk-secret\nnot-an-assignment"),
        ("AI_BACKEND", "unknown"),
        ("AI_CLI_PATH", "/nonexistent/binary"),
        ("CLAUDE_PERMISSION_MODE", "full-access"),
        ("SCHEDULER_INTERVAL", "not-a-number"),
        ("UPDATE_CHECK_INTERVAL", "0"),
        ("REMINDER_MAX_AGE_SECONDS", "0"),
        ("ROBYX_PLATFORM", "matrix"),
        ("ROBYX_WORKSPACE", "{file}"),
        ("KAELOPS_PLATFORM", "matrix"),
        ("KAELOPS_WORKSPACE", "{file}"),
    ],
)
def test_every_known_key_has_an_invalid_value_contract(tmp_path, key, invalid):
    ordinary_file = tmp_path / "not-a-directory"
    ordinary_file.write_text("x")
    invalid = invalid.format(file=ordinary_file)

    with pytest.raises(EnvValidationError) as caught:
        parse_direct_env_updates("%s=%s" % (key, invalid))

    assert caught.value.key in {key, "configuration request"}
    assert invalid not in str(caught.value)


def test_invalid_scheduler_interval_is_intercepted_locally():
    with pytest.raises(EnvValidationError, match="must be an integer"):
        parse_direct_env_updates("SCHEDULER_INTERVAL=abc")


def test_mixed_known_and_unknown_request_is_not_forwardable():
    secret = "sk-must-not-reach-ai"
    with pytest.raises(EnvValidationError) as caught:
        parse_direct_env_updates(
            "OPENAI_API_KEY=%s\nTYPO_SETTING=true" % secret,
        )
    assert secret not in str(caught.value)


def test_natural_language_wrapping_a_secret_assignment_is_intercepted():
    secret = "sk-must-not-reach-ai"
    with pytest.raises(EnvValidationError) as caught:
        parse_direct_env_updates("Please set OPENAI_API_KEY=%s" % secret)
    assert secret not in str(caught.value)


def test_schema_boolean_parser_is_strict_and_normalises():
    field = EnvField(EnvValueKind.BOOLEAN)
    assert field.validate("FLAG", "YES") == "true"
    assert field.validate("FLAG", "0") == "false"
    with pytest.raises(EnvValidationError, match="true or false"):
        field.validate("FLAG", "sometimes")


def test_schema_edge_types_and_typed_conversion(tmp_path):
    required = EnvField(EnvValueKind.STRING)
    optional = EnvField(EnvValueKind.STRING, allow_empty=True)
    assert optional.validate("OPTIONAL", None) == ""
    assert required.validate("COUNT", 42) == "42"
    with pytest.raises(EnvValidationError, match="must not be empty"):
        required.validate("REQUIRED", "")
    with pytest.raises(EnvValidationError, match="single-line"):
        required.validate("REQUIRED", "first\nsecond")

    assert typed_env("SCHEDULER_INTERVAL", "060", as_type=int) == 60
    workspace = tmp_path / "new-workspace"
    assert typed_env("ROBYX_WORKSPACE", str(workspace), as_type=Path) == workspace
    assert typed_env("AI_BACKEND", "codex") == "codex"


def test_schema_rejects_unknown_key_and_kind():
    with pytest.raises(EnvValidationError, match="not a supported setting"):
        typed_env("NOT_A_SETTING", "value")

    invalid_kind = EnvField("future-kind")
    with pytest.raises(EnvValidationError, match="unsupported value type"):
        invalid_kind.validate("FUTURE", "value")


def test_apply_env_updates_rewrites_existing_atomically_and_privately(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AI_BACKEND=claude\n"
        "OPENAI_API_KEY=old\n"
        "# comment\n",
    )
    env_file.chmod(0o644)

    apply_env_updates(env_file, {
        "OPENAI_API_KEY": "new",
        "SCHEDULER_INTERVAL": "42",
    })

    assert env_file.read_text() == (
        "AI_BACKEND=claude\n"
        "OPENAI_API_KEY=new\n"
        "# comment\n"
        "SCHEDULER_INTERVAL=42\n"
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("..env.*.tmp"))


def _write_fake_runtime(project_root: Path) -> Path:
    bot_dir = project_root / "bot"
    bot_dir.mkdir(parents=True)
    config_file = bot_dir / "config.py"
    config_file.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "root = Path(__file__).parent.parent\n"
        "values = {}\n"
        "for line in (root / '.env').read_text().splitlines():\n"
        "    if line and not line.startswith('#') and '=' in line:\n"
        "        key, value = line.split('=', 1)\n"
        "        values[key] = value\n"
        "interval = int(os.environ.get('SCHEDULER_INTERVAL', values.get('SCHEDULER_INTERVAL', '60')))\n"
        "if interval == 13:\n"
        "    raise RuntimeError('candidate refused')\n"
        "(root / 'preflight.pid').write_text(str(os.getpid()))\n",
    )
    return config_file


def test_transaction_preflights_in_fresh_process_and_keeps_candidate(
    tmp_path,
    monkeypatch,
):
    _write_fake_runtime(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"SCHEDULER_INTERVAL=60\n# keep\n")
    parent_pid = os.getpid()
    monkeypatch.setenv("SCHEDULER_INTERVAL", "999")

    result = apply_env_updates_transactionally(
        env_file,
        {"SCHEDULER_INTERVAL": "120"},
        project_root=tmp_path,
    )

    assert result == {"SCHEDULER_INTERVAL": "120"}
    assert env_file.read_bytes() == b"SCHEDULER_INTERVAL=120\n# keep\n"
    assert int((tmp_path / "preflight.pid").read_text()) != parent_pid
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_repository_runtime_config_passes_real_fresh_process_preflight():
    project_root = Path(__file__).parent.parent
    preflight_candidate_config(project_root)


def test_transaction_scrubs_inherited_value_and_rolls_back_byte_for_byte(
    tmp_path,
    monkeypatch,
):
    _write_fake_runtime(tmp_path)
    env_file = tmp_path / ".env"
    original = b"# exact bytes\nSCHEDULER_INTERVAL=60\n\n"
    env_file.write_bytes(original)
    env_file.chmod(0o640)
    # If preflight did not remove this inherited value, the bad candidate
    # would be masked and incorrectly accepted.
    monkeypatch.setenv("SCHEDULER_INTERVAL", "999")

    with pytest.raises(ConfigPreflightError) as caught:
        apply_env_updates_transactionally(
            env_file,
            {"SCHEDULER_INTERVAL": "13"},
            project_root=tmp_path,
        )

    assert "candidate refused" not in str(caught.value)
    assert env_file.read_bytes() == original
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_transaction_removes_new_file_when_preflight_fails(tmp_path):
    project_root = tmp_path / "candidate-project"
    _write_fake_runtime(project_root)
    env_file = project_root / ".env"

    with pytest.raises(ConfigPreflightError):
        apply_env_updates_transactionally(
            env_file,
            {"SCHEDULER_INTERVAL": "13"},
            project_root=project_root,
        )

    assert not env_file.exists()


def test_validation_failure_never_mutates_existing_file(tmp_path):
    env_file = tmp_path / ".env"
    original = b"SCHEDULER_INTERVAL=60\n"
    env_file.write_bytes(original)

    with pytest.raises(EnvValidationError):
        apply_env_updates_transactionally(
            env_file,
            {"SCHEDULER_INTERVAL": "NaN"},
            project_root=tmp_path,
        )

    assert env_file.read_bytes() == original


def test_unexpected_preflight_error_is_redacted_and_restored(
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / ".env"
    original = b"OPENAI_API_KEY=old\n"
    env_file.write_bytes(original)
    secret = "sk-candidate-must-stay-private"

    def fail_with_secret(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(config_updates, "preflight_candidate_config", fail_with_secret)

    with pytest.raises(ConfigPreflightError) as caught:
        apply_env_updates_transactionally(
            env_file,
            {"OPENAI_API_KEY": secret},
            project_root=tmp_path,
        )

    assert secret not in str(caught.value)
    assert env_file.read_bytes() == original


def test_atomic_replace_failure_leaves_original_and_cleans_temp(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    original = b"AI_BACKEND=claude\n"
    env_file.write_bytes(original)

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_updates.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failure"):
        apply_env_updates(env_file, {"AI_BACKEND": "codex"})

    assert env_file.read_bytes() == original
    assert not list(tmp_path.glob("..env.*.tmp"))


def test_rollback_failure_raises_distinct_critical_error(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"SCHEDULER_INTERVAL=60\n")
    monkeypatch.setattr(
        config_updates,
        "preflight_candidate_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConfigPreflightError("preflight failed"),
        ),
    )
    monkeypatch.setattr(
        config_updates,
        "_restore_original",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk failed")),
    )

    with pytest.raises(config_updates.ConfigRollbackError) as caught:
        apply_env_updates_transactionally(
            env_file,
            {"SCHEDULER_INTERVAL": "120"},
            project_root=tmp_path,
        )

    assert "SCHEDULER_INTERVAL=120" not in str(caught.value)


def _load_setup_module():
    setup_path = Path(__file__).parent.parent / "setup.py"
    spec = importlib.util.spec_from_file_location("robyx_setup_for_config_test", setup_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_reuses_schema_and_writes_private_file(tmp_path, monkeypatch):
    setup_module = _load_setup_module()
    env_file = tmp_path / "setup.env"
    monkeypatch.setattr(setup_module, "ENV_FILE", env_file)
    config = {
        "AI_BACKEND": "claude",
        "AI_CLI_PATH": "",
        "CLAUDE_PERMISSION_MODE": "",
        "ROBYX_PLATFORM": "telegram",
        "ROBYX_WORKSPACE": str(tmp_path / "new-workspace"),
        "OPENAI_API_KEY": "",
        "SCHEDULER_INTERVAL": "060",
        "UPDATE_CHECK_INTERVAL": "3600",
        "ROBYX_BOT_TOKEN": "token",
        "COLLAB_PARTICIPANT_POLICY": "read-only",
    }

    setup_module._write_validated_config(config)

    assert "SCHEDULER_INTERVAL=60\n" in env_file.read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_setup_validation_failure_preserves_previous_bytes(tmp_path, monkeypatch):
    setup_module = _load_setup_module()
    env_file = tmp_path / "setup.env"
    original = b"# original\nSCHEDULER_INTERVAL=60\n"
    env_file.write_bytes(original)
    monkeypatch.setattr(setup_module, "ENV_FILE", env_file)

    with pytest.raises(EnvValidationError, match="SCHEDULER_INTERVAL"):
        setup_module._write_validated_config({"SCHEDULER_INTERVAL": "broken"})

    assert env_file.read_bytes() == original


def test_base_exception_preflight_always_restores_candidate(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    original = b"SCHEDULER_INTERVAL=60\n"
    env_file.write_bytes(original)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(config_updates, "preflight_candidate_config", interrupted)
    with pytest.raises(KeyboardInterrupt):
        apply_env_updates_transactionally(
            env_file,
            {"SCHEDULER_INTERVAL": "120"},
            project_root=tmp_path,
        )

    assert env_file.read_bytes() == original


def test_preflight_timeout_terminates_group_and_reaps(tmp_path):
    proc = MagicMock()
    proc.pid = 424242
    proc.poll.return_value = None
    proc.communicate.side_effect = subprocess.TimeoutExpired("python", 1)
    proc.wait.side_effect = [subprocess.TimeoutExpired("python", 1), 0]

    with patch("config_updates.subprocess.Popen", return_value=proc), \
         patch("config_updates.os.killpg") as killpg:
        with pytest.raises(subprocess.TimeoutExpired):
            config_updates._run_preflight_process(
                ["python"],
                cwd=tmp_path,
                env={},
                timeout=1,
            )

    assert [item.args for item in killpg.call_args_list] == [
        (proc.pid, signal.SIGTERM),
        (proc.pid, signal.SIGKILL),
    ]
    assert proc.wait.call_count == 2


def test_windows_preflight_cleanup_uses_taskkill_tree():
    proc = MagicMock()
    proc.pid = 424242
    proc.poll.return_value = None
    proc.wait.return_value = 0

    with patch.object(config_updates.sys, "platform", "win32"), \
         patch("config_updates.subprocess.run") as run:
        config_updates._terminate_preflight_process(proc)

    run.assert_called_once_with(
        ["taskkill", "/PID", "424242", "/T", "/F"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=3.0,
    )
    proc.terminate.assert_not_called()
    proc.wait.assert_called_once_with(timeout=1.0)
