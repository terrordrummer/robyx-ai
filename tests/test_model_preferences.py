"""Tests for ``bot.model_preferences`` — backend-aware alias resolution.

Covers the three resolution paths used at runtime:

1. Caller-supplied model wins (after legacy ``haiku``/``sonnet``/``opus``
   normalisation).
2. Agent's stored preference is consulted when the caller passes ``None``.
3. Role default from ``models.yaml`` (or the env / hard-coded fallback)
   takes over when neither is set.

The fixture overrides the alias table so the assertions stay independent
from whatever ``models.yaml`` is shipped in the repo.
"""

import importlib
import logging
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# L4: startup fallback logging
# ---------------------------------------------------------------------------


class TestModelsYamlFallbackLogging:
    """``config._log_models_fallback_source`` is the startup diagnostic that
    tells the user which model-preference layer is active. Without it a
    fresh clone with no ``models.yaml`` would silently bill the hardcoded
    default tier — a nasty surprise. These tests lock in the three
    branches of the decision tree so future refactors cannot regress the
    visibility contract."""

    def test_models_yaml_present_logs_path(self, tmp_path, caplog):
        from config import _log_models_fallback_source

        models_file = tmp_path / "models.yaml"
        models_file.write_text("defaults: {orchestrator: sonnet}\n")

        logger = logging.getLogger("test.models.present")
        with caplog.at_level(logging.INFO, logger=logger.name):
            _log_models_fallback_source(
                models_config={"defaults": {"orchestrator": "sonnet"}},
                models_file=models_file,
                yaml_available=True,
                env_defaults="",
                env_aliases="",
                logger=logger,
            )

        msgs = [r.message for r in caplog.records if r.name == logger.name]
        assert any("loaded from" in m and str(models_file) in m for m in msgs)
        assert not any("fallback" in m.lower() for m in msgs)

    def test_missing_file_with_env_override(self, tmp_path, caplog):
        from config import _log_models_fallback_source

        missing = tmp_path / "nope.yaml"
        logger = logging.getLogger("test.models.env")

        with caplog.at_level(logging.INFO, logger=logger.name):
            _log_models_fallback_source(
                models_config={},
                models_file=missing,
                yaml_available=True,
                env_defaults='{"orchestrator":"opus"}',
                env_aliases="",
                logger=logger,
            )

        msgs = [r.message for r in caplog.records if r.name == logger.name]
        assert any(
            "not found" in m and "AI_MODEL_* env vars" in m for m in msgs
        )

    def test_missing_file_no_env_logs_hardcoded_tier(self, tmp_path, caplog):
        from config import _log_models_fallback_source, DEFAULT_MODEL_DEFAULTS

        missing = tmp_path / "nope.yaml"
        logger = logging.getLogger("test.models.hardcoded")

        with caplog.at_level(logging.INFO, logger=logger.name):
            _log_models_fallback_source(
                models_config={},
                models_file=missing,
                yaml_available=True,
                env_defaults="",
                env_aliases="",
                logger=logger,
            )

        msgs = [r.message for r in caplog.records if r.name == logger.name]
        joined = "\n".join(msgs)
        assert "not found" in joined
        assert "hardcoded defaults" in joined
        # Must name each role's default so an operator scanning bot.log can
        # immediately see which tier they are billing.
        assert DEFAULT_MODEL_DEFAULTS["orchestrator"] in joined
        assert DEFAULT_MODEL_DEFAULTS["workspace"] in joined
        assert DEFAULT_MODEL_DEFAULTS["specialist"] in joined

    def test_pyyaml_missing_reports_specific_reason(self, tmp_path, caplog):
        from config import _log_models_fallback_source

        existing = tmp_path / "models.yaml"
        existing.write_text("defaults: {}\n")  # file exists but yaml lib absent
        logger = logging.getLogger("test.models.noyaml")

        with caplog.at_level(logging.INFO, logger=logger.name):
            _log_models_fallback_source(
                models_config={},
                models_file=existing,
                yaml_available=False,
                env_defaults="",
                env_aliases="",
                logger=logger,
            )

        msgs = [r.message for r in caplog.records if r.name == logger.name]
        assert any("PyYAML not installed" in m for m in msgs)


@pytest.fixture
def configured_aliases(monkeypatch):
    """Install a deterministic alias table on the config module.

    Patches both the live attribute on ``config`` and the already-imported
    copies inside ``model_preferences`` so the resolver sees the test
    table regardless of import order.
    """
    import config
    import model_preferences as mp

    defaults = {
        "orchestrator": "balanced",
        "workspace": "balanced",
        "specialist": "powerful",
        "scheduled": "fast",
        "one-shot": "fast",
    }
    aliases = {
        "fast": {
            "claude": "haiku",
            "codex": "gpt-5-mini",
            "opencode": "openai/gpt-5-mini",
        },
        "balanced": {
            "claude": "sonnet",
            "codex": "gpt-5",
            "opencode": "openai/gpt-5",
        },
        "powerful": {
            "claude": "opus",
            "codex": "gpt-5.4",
            "opencode": "openai/gpt-5.4",
        },
    }

    monkeypatch.setattr(config, "AI_MODEL_DEFAULTS", defaults, raising=False)
    monkeypatch.setattr(config, "AI_MODEL_ALIASES", aliases, raising=False)
    monkeypatch.setattr(config, "AI_BACKEND", "claude", raising=False)
    monkeypatch.setattr(mp, "AI_MODEL_DEFAULTS", defaults, raising=False)
    monkeypatch.setattr(mp, "AI_MODEL_ALIASES", aliases, raising=False)
    monkeypatch.setattr(mp, "AI_BACKEND", "claude", raising=False)
    return mp


# ---------------------------------------------------------------------------
# get_backend_key
# ---------------------------------------------------------------------------


class TestGetBackendKey:
    def test_string_is_lowercased(self, configured_aliases):
        assert configured_aliases.get_backend_key("Claude") == "claude"

    def test_instance_with_key_attribute(self, configured_aliases):
        class B:
            key = "Codex"

        assert configured_aliases.get_backend_key(B()) == "codex"

    def test_instance_uses_name_first_word(self, configured_aliases):
        class B:
            name = "OpenCode CLI 0.9"

        assert configured_aliases.get_backend_key(B()) == "opencode"

    def test_falls_back_to_class_name(self, configured_aliases):
        class CustomBackend:
            pass

        assert configured_aliases.get_backend_key(CustomBackend()) == "custom"


# ---------------------------------------------------------------------------
# get_default_model_preference
# ---------------------------------------------------------------------------


class TestGetDefaultModelPreference:
    def test_known_role_returns_configured_default(self, configured_aliases):
        assert configured_aliases.get_default_model_preference("specialist") == "powerful"
        assert configured_aliases.get_default_model_preference("scheduled") == "fast"

    def test_unknown_role_falls_back_to_workspace(self, configured_aliases):
        assert configured_aliases.get_default_model_preference("ghost") == "balanced"

    def test_none_role_uses_workspace(self, configured_aliases):
        assert configured_aliases.get_default_model_preference(None) == "balanced"

    def test_balanced_safety_net_when_table_is_empty(self, monkeypatch):
        import config
        import model_preferences as mp

        monkeypatch.setattr(config, "AI_MODEL_DEFAULTS", {}, raising=False)
        monkeypatch.setattr(mp, "AI_MODEL_DEFAULTS", {}, raising=False)
        assert mp.get_default_model_preference("workspace") == "balanced"


# ---------------------------------------------------------------------------
# resolve_model_preference
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Minimal backend stand-in: only ``name`` matters for the resolver."""
    def __init__(self, name):
        self.name = name


class TestResolveModelPreference:
    def test_explicit_alias_resolves_per_backend(self, configured_aliases):
        assert configured_aliases.resolve_model_preference(
            "balanced", _FakeBackend("Claude Code"),
        ) == "sonnet"
        assert configured_aliases.resolve_model_preference(
            "balanced", _FakeBackend("OpenCode"),
        ) == "openai/gpt-5"
        assert configured_aliases.resolve_model_preference(
            "balanced", _FakeBackend("Codex CLI"),
        ) == "gpt-5"

    def test_legacy_claude_names_are_remapped(self, configured_aliases):
        # Old tasks.md rows still use "haiku"/"sonnet"/"opus" — they MUST
        # keep working without code changes.
        assert configured_aliases.resolve_model_preference(
            "haiku", _FakeBackend("Codex"),
        ) == "gpt-5-mini"
        assert configured_aliases.resolve_model_preference(
            "opus", _FakeBackend("OpenCode"),
        ) == "openai/gpt-5.4"

    def test_role_default_is_used_when_model_is_none(self, configured_aliases):
        # ``specialist`` role default → "powerful" → opus on Claude.
        assert configured_aliases.resolve_model_preference(
            None, _FakeBackend("Claude"), role="specialist",
        ) == "opus"

    def test_explicit_concrete_model_passes_through(self, configured_aliases):
        # A user-supplied literal id is returned unchanged so power users
        # can pin a specific provider/model in their tasks.md row.
        assert configured_aliases.resolve_model_preference(
            "openai/gpt-5.4-preview", _FakeBackend("OpenCode"),
        ) == "openai/gpt-5.4-preview"

    def test_unknown_alias_passes_through(self, configured_aliases):
        # A non-alias, non-empty string is returned verbatim — Robyx
        # never throws KeyError on a misspelt alias.
        assert configured_aliases.resolve_model_preference(
            "wizardly", _FakeBackend("Claude"),
        ) == "wizardly"

    def test_empty_string_falls_through_to_default(self, configured_aliases):
        assert configured_aliases.resolve_model_preference(
            "   ", _FakeBackend("Claude"), role="orchestrator",
        ) == "sonnet"

    def test_falls_back_to_active_backend_when_target_missing(
        self, monkeypatch, configured_aliases
    ):
        # When the alias table has an entry for the active AI_BACKEND but
        # not for the backend instance Robyx was handed, the resolver
        # should still pick the AI_BACKEND fallback rather than returning
        # the alias itself.
        import config
        import model_preferences as mp

        aliases = {
            "balanced": {
                "claude": "sonnet",
                # Note: no entry for "exotic"
            },
        }
        monkeypatch.setattr(config, "AI_MODEL_ALIASES", aliases, raising=False)
        monkeypatch.setattr(mp, "AI_MODEL_ALIASES", aliases, raising=False)
        monkeypatch.setattr(config, "AI_BACKEND", "claude", raising=False)
        monkeypatch.setattr(mp, "AI_BACKEND", "claude", raising=False)

        assert mp.resolve_model_preference(
            "balanced", _FakeBackend("Exotic Engine"),
        ) == "sonnet"


# ---------------------------------------------------------------------------
# config.yaml loader (smoke test)
# ---------------------------------------------------------------------------


class TestConfigYamlLoader:
    """``models.yaml`` is read at import time. We smoke-test the loader by
    invoking the helpers directly so we don't depend on a fresh import."""

    def test_load_yaml_file_returns_dict(self, tmp_path):
        import config

        yaml_path = tmp_path / "models.yaml"
        yaml_path.write_text("aliases:\n  fast:\n    claude: haiku\n")
        loaded = config._load_yaml_file(yaml_path)
        assert loaded == {"aliases": {"fast": {"claude": "haiku"}}}

    def test_load_yaml_file_missing_returns_empty(self, tmp_path):
        import config

        assert config._load_yaml_file(tmp_path / "nope.yaml") == {}

    def test_load_yaml_file_invalid_returns_empty(self, tmp_path):
        import config

        bad = tmp_path / "bad.yaml"
        bad.write_text(":\n  - this is not\n: valid")
        # Malformed YAML must NOT crash the loader.
        result = config._load_yaml_file(bad)
        assert isinstance(result, dict)

    def test_load_json_env_falls_back_on_invalid(self, monkeypatch):
        import config

        monkeypatch.setenv("ROBYX_TEST_BAD_JSON", "{not json")
        assert config._load_json_env(
            "ROBYX_TEST_BAD_JSON", {"default": True},
        ) == {"default": True}

    def test_load_json_env_returns_default_when_unset(self, monkeypatch):
        import config

        monkeypatch.delenv("ROBYX_TEST_MISSING", raising=False)
        assert config._load_json_env("ROBYX_TEST_MISSING", []) == []

    def test_load_json_env_parses_valid_json(self, monkeypatch):
        import config

        monkeypatch.setenv("ROBYX_TEST_GOOD", '{"a": 1}')
        assert config._load_json_env("ROBYX_TEST_GOOD", {}) == {"a": 1}
