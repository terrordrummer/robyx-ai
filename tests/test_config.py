"""Tests for pure helpers in bot/config.py."""

from config import _int_env


def test_int_env_returns_parsed_integer(monkeypatch):
    monkeypatch.setenv("ROBYX_TEST_INT", "42")
    assert _int_env("ROBYX_TEST_INT", "KAELOPS_TEST_INT") == 42


def test_int_env_returns_zero_not_none_for_zero_value(monkeypatch):
    """Regression: `int(raw) or None` silently turned 0 into None."""
    monkeypatch.setenv("ROBYX_TEST_INT", "0")
    assert _int_env("ROBYX_TEST_INT", "KAELOPS_TEST_INT") == 0


def test_int_env_returns_negative_integer(monkeypatch):
    monkeypatch.setenv("ROBYX_TEST_INT", "-100123")
    assert _int_env("ROBYX_TEST_INT", "KAELOPS_TEST_INT") == -100123


def test_int_env_falls_back_to_legacy_key(monkeypatch):
    monkeypatch.delenv("ROBYX_TEST_INT", raising=False)
    monkeypatch.setenv("KAELOPS_TEST_INT", "7")
    assert _int_env("ROBYX_TEST_INT", "KAELOPS_TEST_INT") == 7


def test_int_env_returns_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("ROBYX_TEST_INT", raising=False)
    monkeypatch.delenv("KAELOPS_TEST_INT", raising=False)
    assert _int_env("ROBYX_TEST_INT", "KAELOPS_TEST_INT", default=99) == 99


def test_int_env_returns_none_on_non_numeric(monkeypatch):
    monkeypatch.setenv("ROBYX_TEST_INT", "not-a-number")
    assert _int_env("ROBYX_TEST_INT", "KAELOPS_TEST_INT") is None
