from pathlib import Path

import config


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text()


def test_env_example_lists_cross_platform_config_keys():
    contents = _read(".env.example")

    expected_keys = [
        "ROBYX_PLATFORM=",
        "AI_BACKEND=",
        "AI_CLI_PATH=",
        "CLAUDE_PERMISSION_MODE=",
        "ROBYX_WORKSPACE=",
        "OPENAI_API_KEY=",
        "SCHEDULER_INTERVAL=",
        "UPDATE_CHECK_INTERVAL=",
        "ROBYX_BOT_TOKEN=",
        "ROBYX_CHAT_ID=",
        "ROBYX_OWNER_ID=",
        "SLACK_BOT_TOKEN=",
        "SLACK_APP_TOKEN=",
        "SLACK_CHANNEL_ID=",
        "SLACK_OWNER_ID=",
        "DISCORD_BOT_TOKEN=",
        "DISCORD_GUILD_ID=",
        "DISCORD_CONTROL_CHANNEL_ID=",
        "DISCORD_OWNER_ID=",
    ]

    for key in expected_keys:
        assert key in contents


def test_readme_documents_current_cross_platform_contract():
    contents = _read("README.md")

    assert "SCHEDULER_INTERVAL" in contents
    assert "CLAUDE_PERMISSION_MODE" in contents
    assert "SLACK_APP_TOKEN" in contents
    assert "DISCORD_CONTROL_CHANNEL_ID" in contents
    assert "relay the parsed result back into the target topic/channel" in contents
    assert "one Telegram group" not in contents
    assert "Agent logs result to log.txt" not in contents
    assert "one manual step" not in contents


def test_orchestrator_documents_current_cross_platform_contract():
    contents = _read("ORCHESTRATOR.md")

    assert "AI_CLI_PATH" in contents
    assert "CLAUDE_PERMISSION_MODE" in contents
    assert "ROBYX_WORKSPACE" in contents
    assert "SCHEDULER_INTERVAL" in contents
    assert "SLACK_APP_TOKEN" in contents
    assert "DISCORD_CONTROL_CHANNEL_ID" in contents
    assert "post their result back into the target workspace topic/channel" in contents
    assert "one token to copy" not in contents


def test_robyx_prompt_mentions_full_platform_key_set():
    prompt = config.ROBYX_SYSTEM_PROMPT

    assert "AI_CLI_PATH" in prompt
    assert "CLAUDE_PERMISSION_MODE" in prompt
    assert "ROBYX_WORKSPACE" in prompt
    assert "SLACK_APP_TOKEN" in prompt
    assert "DISCORD_CONTROL_CHANNEL_ID" in prompt
    assert "data/queue.json" in prompt
    assert "manage a staff of AI agents through Telegram." not in prompt
    assert "tasks.md entry automatically." not in prompt
    assert "ONE manual step" not in prompt
