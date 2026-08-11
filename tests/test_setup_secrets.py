import importlib.util
import os
from pathlib import Path

import pytest


def _load_setup_module():
    setup_path = Path(__file__).parent.parent / "setup.py"
    spec = importlib.util.spec_from_file_location("robyx_setup_for_secret_test", setup_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup_module():
    return _load_setup_module()


def _private_secret_file(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(value + "\n")
    if os.name == "posix":
        path.chmod(0o600)
    return path


@pytest.mark.parametrize(
    ("option", "attribute"),
    [
        ("--bot-token-file", "bot_token"),
        ("--slack-bot-token-file", "slack_bot_token"),
        ("--slack-app-token-file", "slack_app_token"),
        ("--discord-bot-token-file", "discord_bot_token"),
        ("--openai-key-file", "openai_key"),
    ],
)
def test_every_setup_secret_accepts_private_file(
    setup_module, tmp_path, option, attribute
):
    secret = "never-in-argv-%s" % attribute
    source = _private_secret_file(tmp_path, attribute, secret)
    args = setup_module.parse_args([option, str(source)])

    setup_module.prepare_secret_inputs(args, environ={})

    assert getattr(args, attribute) == secret


def test_setup_secret_accepts_setup_only_environment_without_warning(setup_module):
    args = setup_module.parse_args([])
    warnings = []

    setup_module.prepare_secret_inputs(
        args,
        environ={"ROBYX_SETUP_DISCORD_BOT_TOKEN": "discord-from-env"},
        warn=warnings.append,
    )

    assert args.discord_bot_token == "discord-from-env"
    assert warnings == []


def test_legacy_argv_secret_warns_without_echoing_value(setup_module):
    secret = "must-not-be-printed"
    args = setup_module.parse_args(["--bot-token", secret])
    warnings = []

    setup_module.prepare_secret_inputs(args, environ={}, warn=warnings.append)

    assert args.bot_token == secret
    assert len(warnings) == 1
    assert "deprecated" in warnings[0]
    assert secret not in warnings[0]


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_group_readable_secret_file_is_rejected_without_value(setup_module, tmp_path):
    secret = "must-not-leak"
    source = _private_secret_file(tmp_path, "token", secret)
    source.chmod(0o640)
    args = setup_module.parse_args(["--bot-token-file", str(source)])

    with pytest.raises(setup_module.SecretInputError) as caught:
        setup_module.prepare_secret_inputs(args, environ={})

    assert "group or others" in str(caught.value)
    assert secret not in str(caught.value)


def test_symlinked_secret_file_is_rejected_without_following(setup_module, tmp_path):
    secret = "must-not-leak"
    target = _private_secret_file(tmp_path, "actual", secret)
    link = tmp_path / "linked"
    link.symlink_to(target)
    args = setup_module.parse_args(["--openai-key-file", str(link)])

    with pytest.raises(setup_module.SecretInputError, match="symlinks") as caught:
        setup_module.prepare_secret_inputs(args, environ={})

    assert secret not in str(caught.value)


def test_conflicting_secret_sources_fail_closed(setup_module, tmp_path):
    source = _private_secret_file(tmp_path, "token", "file-value")
    args = setup_module.parse_args(
        ["--bot-token", "argv-value", "--bot-token-file", str(source)]
    )

    with pytest.raises(setup_module.SecretInputError, match="only one source"):
        setup_module.prepare_secret_inputs(args, environ={})
