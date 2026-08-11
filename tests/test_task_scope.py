from types import SimpleNamespace

import pytest

import config
from messaging.base import ChatRef
from task_scope import (
    TaskScope,
    attach_scope,
    legacy_scope_matches,
    scope_from_record,
)


def test_task_scope_round_trip_preserves_none_parent():
    scope = TaskScope.from_chat_ref(ChatRef("telegram", "-1001"), None)

    assert TaskScope.from_dict(scope.to_dict()) == scope
    assert scope.parent_thread_id is None


def test_task_scope_never_infers_platform_from_chat_id():
    with pytest.raises(ValueError):
        TaskScope(platform="", chat_id="123:456", parent_thread_id=9)

    with pytest.raises(ValueError):
        TaskScope(platform="telegram", chat_id=None, parent_thread_id=9)
    with pytest.raises(ValueError):
        TaskScope(platform="telegram", chat_id="  ", parent_thread_id=9)


def test_from_dict_rejects_non_mapping():
    with pytest.raises(ValueError, match="object"):
        TaskScope.from_dict([])


def test_discord_child_channel_updates_chat_and_parent_components():
    parent = TaskScope("discord", "123:10", 10)

    assert parent.for_parent_channel(77) == TaskScope("discord", "123:77", 77)


def test_telegram_and_raw_slack_child_channel_rules():
    telegram = TaskScope("telegram", "-1001", 10)
    assert telegram.for_parent_channel(77) == TaskScope(
        "telegram", "-1001", 77,
    )
    slack = TaskScope("slack", "C01", None)
    assert slack.for_parent_channel("C01") == TaskScope("slack", "C01", "C01")
    with pytest.raises(ValueError, match="non-canonical"):
        slack.for_parent_channel("C02")


def test_record_helpers_distinguish_legacy_and_attach_canonical():
    scope = TaskScope("telegram", "-1001", 42)
    record = {}
    assert scope_from_record(record) is None
    assert attach_scope(record, scope) is record
    assert scope_from_record(record) == scope


def test_present_invalid_scope_fails_closed():
    with pytest.raises(ValueError):
        scope_from_record({"workspace_scope": {"chat_id": "1"}})


@pytest.mark.parametrize("chat_id", ["-1001", "-1002"])
def test_legacy_none_parent_is_always_ambiguous(chat_id):
    current = TaskScope("telegram", chat_id, None)

    assert not legacy_scope_matches(
        {}, current, legacy_parent_thread_id=None, manager=SimpleNamespace(),
    )


def test_legacy_exact_platform_chat_evidence_is_unambiguous():
    current = TaskScope("telegram", "-1001", 42)

    assert legacy_scope_matches(
        {"platform": "telegram", "chat_id": "-1001"},
        current,
        legacy_parent_thread_id="42",
    )
    assert not legacy_scope_matches(
        {"platform": "telegram", "chat_id": "-1002"},
        current,
        legacy_parent_thread_id="42",
    )
    assert not legacy_scope_matches(
        {"platform": "unknown", "chat_id": "-1001"},
        current,
        legacy_parent_thread_id="42",
    )


def test_legacy_fallback_requires_manager_and_handles_manager_failure():
    current = TaskScope("telegram", "-1001", 42)
    assert not legacy_scope_matches(
        {}, current, legacy_parent_thread_id=42,
    )
    manager = SimpleNamespace(
        list_active=lambda: (_ for _ in ()).throw(RuntimeError("broken")),
    )
    assert not legacy_scope_matches(
        {}, current, legacy_parent_thread_id=42, manager=manager,
    )


def test_legacy_host_fallback_requires_unique_manager_owner(monkeypatch):
    monkeypatch.setattr(config, "PLATFORM", "telegram")
    monkeypatch.setattr(config, "CHAT_ID", -1001)
    current = TaskScope("telegram", "-1001", 42)
    owner = SimpleNamespace(name="ops", thread_id=42)
    manager = SimpleNamespace(list_active=lambda: [owner])

    assert legacy_scope_matches(
        {}, current, legacy_parent_thread_id=42, manager=manager,
    )

    collision = SimpleNamespace(name="other", thread_id="42")
    manager = SimpleNamespace(list_active=lambda: [owner, collision])
    assert not legacy_scope_matches(
        {}, current, legacy_parent_thread_id=42, manager=manager,
    )


def test_legacy_host_fallback_rejects_foreign_chat_and_slack(monkeypatch):
    owner = SimpleNamespace(name="ops", thread_id=42)
    manager = SimpleNamespace(list_active=lambda: [owner])
    monkeypatch.setattr(config, "PLATFORM", "telegram")
    monkeypatch.setattr(config, "CHAT_ID", -1001)

    assert not legacy_scope_matches(
        {}, TaskScope("telegram", "-1002", 42),
        legacy_parent_thread_id=42,
        manager=manager,
    )


def test_legacy_discord_host_fallback(monkeypatch):
    owner = SimpleNamespace(name="ops", thread_id=42)
    manager = SimpleNamespace(list_active=lambda: [owner])
    monkeypatch.setattr(config, "PLATFORM", "discord")
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", 123)

    assert legacy_scope_matches(
        {}, TaskScope("discord", "123:42", 42),
        legacy_parent_thread_id=42,
        manager=manager,
    )
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", 0)
    assert not legacy_scope_matches(
        {}, TaskScope("discord", "123:42", 42),
        legacy_parent_thread_id=42,
        manager=manager,
    )


def test_legacy_fallback_rejects_configured_platform_mismatch(monkeypatch):
    owner = SimpleNamespace(name="ops", thread_id=42)
    manager = SimpleNamespace(list_active=lambda: [owner])
    monkeypatch.setattr(config, "PLATFORM", "discord")

    assert not legacy_scope_matches(
        {}, TaskScope("telegram", "-1001", 42),
        legacy_parent_thread_id=42,
        manager=manager,
    )

    monkeypatch.setattr(config, "PLATFORM", "slack")
    assert not legacy_scope_matches(
        {}, TaskScope("slack", "C01", 42),
        legacy_parent_thread_id=42,
        manager=manager,
    )
