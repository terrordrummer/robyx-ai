from pathlib import Path

from config_updates import apply_env_updates, parse_direct_env_updates


def test_parse_direct_env_updates_accepts_explicit_assignments():
    updates = parse_direct_env_updates(
        "OPENAI_API_KEY=sk-test\nDISCORD_CONTROL_CHANNEL_ID=123456789\nCLAUDE_PERMISSION_MODE=bypassPermissions",
    )

    assert updates == {
        "OPENAI_API_KEY": "sk-test",
        "DISCORD_CONTROL_CHANNEL_ID": "123456789",
        "CLAUDE_PERMISSION_MODE": "bypassPermissions",
    }


def test_parse_direct_env_updates_rejects_natural_language():
    assert parse_direct_env_updates("here is the key: sk-test") == {}


def test_apply_env_updates_rewrites_existing_and_appends_new(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AI_BACKEND=claude\n"
        "OPENAI_API_KEY=old\n"
        "# comment\n",
    )

    apply_env_updates(env_file, {
        "OPENAI_API_KEY": "new",
        "DISCORD_OWNER_ID": "42",
    })

    assert env_file.read_text() == (
        "AI_BACKEND=claude\n"
        "OPENAI_API_KEY=new\n"
        "# comment\n"
        "DISCORD_OWNER_ID=42\n"
    )


def test_parse_direct_env_updates_rejects_nonexistent_ai_cli_path():
    """AI_CLI_PATH must point to an existing executable."""
    assert parse_direct_env_updates("AI_CLI_PATH=/nonexistent/binary") == {}


def test_parse_direct_env_updates_accepts_valid_ai_cli_path(tmp_path):
    """AI_CLI_PATH pointing to a real executable should be accepted."""
    import os
    script = tmp_path / "my-cli"
    script.write_text("#!/bin/sh\necho ok\n")
    script.chmod(0o755)
    result = parse_direct_env_updates("AI_CLI_PATH=%s" % script)
    assert result == {"AI_CLI_PATH": str(script)}
