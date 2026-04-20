"""Robyx — Configuration loader.

All configuration comes from .env — no hardcoded paths, tokens, or IDs.
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

try:  # PyYAML is optional at import time so the test suite can stub it.
    import yaml  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only when dep is missing
    yaml = None  # type: ignore[assignment]

_log = logging.getLogger("robyx.config")

# Project root is one level up from bot/
PROJECT_ROOT = Path(__file__).parent.parent
BOT_DIR = Path(__file__).parent


TEMPLATES_DIR = PROJECT_ROOT / "templates"


def _load_prompt(filename: str) -> str:
    """Load a system prompt from ``templates/``, stripped of surrounding blanks.

    Prompts are stored as standalone ``.md`` files so they can be diffed,
    edited, and version-controlled without touching Python. The three
    canonical ones (orchestrator, workspace agent, focused agent) are
    required at import time; other callers may add more over time.
    """
    return (TEMPLATES_DIR / filename).read_text().strip()


load_dotenv(PROJECT_ROOT / ".env")


def _env(new_key, old_key, default=None):
    """Read an env var with legacy-name fallback for kael-ops → robyx migration."""
    return os.environ.get(new_key) or os.environ.get(old_key) or default


def _int_env(new_key: str, old_key: str, default: int = 0):
    """Read an integer env var with legacy fallback.  Non-numeric values → None."""
    raw = _env(new_key, old_key, str(default))
    try:
        return int(raw)
    except (ValueError, TypeError):
        _log.warning("Non-integer value for %s/%s: %r — using None", new_key, old_key, raw)
        return None


# ── Required ──
BOT_TOKEN = _env("ROBYX_BOT_TOKEN", "KAELOPS_BOT_TOKEN")
CHAT_ID = _int_env("ROBYX_CHAT_ID", "KAELOPS_CHAT_ID")
OWNER_ID = _int_env("ROBYX_OWNER_ID", "KAELOPS_OWNER_ID")
AI_BACKEND = os.environ.get("AI_BACKEND", "claude")
AI_CLI_PATH = os.environ.get("AI_CLI_PATH", "")  # empty → auto-detected lazily in ai_backend.create_backend()
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "").strip()
PLATFORM = _env("ROBYX_PLATFORM", "KAELOPS_PLATFORM", "telegram")

# ── Slack ──
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")  # xoxb-...
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")   # xapp-... (Socket Mode)
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")  # control room channel ID
SLACK_OWNER_ID = os.environ.get("SLACK_OWNER_ID", "")

# ── Discord (used when PLATFORM=discord) ──
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = _int_env("DISCORD_GUILD_ID", "", 0)
DISCORD_OWNER_ID = _int_env("DISCORD_OWNER_ID", "", 0)
DISCORD_CONTROL_CHANNEL_ID = _int_env("DISCORD_CONTROL_CHANNEL_ID", "", 0)

# ── Optional ──
WORKSPACE = Path(_env("ROBYX_WORKSPACE", "KAELOPS_WORKSPACE", os.path.expanduser("~/Workspace")))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
SCHEDULER_INTERVAL = int(os.environ.get("SCHEDULER_INTERVAL", "60"))  # unified scheduler tick
UPDATE_CHECK_INTERVAL = int(os.environ.get("UPDATE_CHECK_INTERVAL", "3600"))  # 1 hour


# ── Model preferences ─────────────────────────────────────────────────────
#
# Model selection lives in ``models.yaml`` at the repo root. Robyx loads
# it once at startup, with two layers of fallback:
#
#   1. ``models.yaml``                       (preferred — versioned, shared)
#   2. ``AI_MODEL_DEFAULTS`` / ``AI_MODEL_ALIASES`` env vars  (JSON; legacy)
#   3. Hard-coded ``DEFAULT_MODEL_*``        (always-safe baseline)
#
# This means the bot still boots even on a brand-new clone with no
# ``models.yaml``, while letting power users override per-machine via env.

def _load_json_env(name: str, default: dict) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _log.warning("Ignoring malformed JSON in env var %s", name)
        return default


def _load_yaml_file(path: Path) -> dict:
    if not path.exists() or yaml is None:
        return {}
    try:
        loaded = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:  # type: ignore[attr-defined]
        _log.warning("Failed to parse %s: %s", path, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


DEFAULT_MODEL_DEFAULTS = {
    "orchestrator": "balanced",
    "workspace": "balanced",
    "specialist": "powerful",
    "scheduled": "fast",
    "one-shot": "fast",
}

DEFAULT_MODEL_ALIASES = {
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

MODELS_CONFIG_FILE = PROJECT_ROOT / "models.yaml"
_models_config = _load_yaml_file(MODELS_CONFIG_FILE)


def _log_models_fallback_source(
    models_config: dict,
    models_file: Path,
    yaml_available: bool,
    env_defaults: str,
    env_aliases: str,
    logger: logging.Logger = _log,
) -> None:
    """Log which model-preference layer is actually in effect.

    Surfaces silent surprises about *which model is being billed* to the
    log file at startup. Three cases:
      - models.yaml present and parsed → log the path.
      - models.yaml missing but env JSON present → log env override and reason.
      - neither → log hardcoded defaults and reason, so a fresh clone with
        no config doesn't quietly bill the wrong tier.
    Extracted so it can be exercised deterministically by the test suite.
    """
    if models_config:
        logger.info("Model preferences loaded from %s", models_file)
        return

    if not models_file.exists():
        reason = "not found"
    elif not yaml_available:
        reason = "PyYAML not installed"
    else:
        reason = "empty or unparseable"

    if env_defaults.strip() or env_aliases.strip():
        logger.info(
            "models.yaml %s at %s — falling back to AI_MODEL_* env vars",
            reason, models_file,
        )
    else:
        logger.info(
            "models.yaml %s at %s — falling back to hardcoded defaults "
            "(orchestrator=%s, workspace=%s, specialist=%s)",
            reason, models_file,
            DEFAULT_MODEL_DEFAULTS["orchestrator"],
            DEFAULT_MODEL_DEFAULTS["workspace"],
            DEFAULT_MODEL_DEFAULTS["specialist"],
        )


_log_models_fallback_source(
    _models_config,
    MODELS_CONFIG_FILE,
    yaml is not None,
    os.environ.get("AI_MODEL_DEFAULTS", ""),
    os.environ.get("AI_MODEL_ALIASES", ""),
)

AI_MODEL_DEFAULTS = _models_config.get(
    "defaults",
    _load_json_env("AI_MODEL_DEFAULTS", DEFAULT_MODEL_DEFAULTS),
)
AI_MODEL_ALIASES = _models_config.get(
    "aliases",
    _load_json_env("AI_MODEL_ALIASES", DEFAULT_MODEL_ALIASES),
)


# ── Paths ──
DATA_DIR = PROJECT_ROOT / "data"
STATE_FILE = DATA_DIR / "state.json"
TASKS_FILE = DATA_DIR / "tasks.md"
SPECIALISTS_FILE = DATA_DIR / "specialists.md"
LOG_FILE = PROJECT_ROOT / "bot.log"
AGENTS_DIR = DATA_DIR / "agents"
SPECIALISTS_DIR = DATA_DIR / "specialists"
TIMED_QUEUE_FILE = DATA_DIR / "timed_queue.json"  # legacy — kept for migration
QUEUE_FILE = DATA_DIR / "queue.json"
CONTINUOUS_DIR = DATA_DIR / "continuous"
VERSION_FILE = PROJECT_ROOT / "VERSION"
RELEASES_DIR = PROJECT_ROOT / "releases"
UPDATES_STATE_FILE = DATA_DIR / "updates.json"

# ── Limits ──
MAX_MESSAGE_LEN = 4000
MAX_AI_RETRIES = 3
AI_IDLE_TIMEOUT = int(
    os.environ.get("AI_IDLE_TIMEOUT", "600")
)  # streaming-path liveness: max seconds without any output from the AI
#  subprocess before we treat it as hung and kill. A responsive agent that
#  keeps emitting stream-json lines stays alive indefinitely (up to
#  AI_TIMEOUT). Default 10 min covers long tool calls (e.g. heavy pytest
#  runs) while still catching real hangs.
AI_TIMEOUT = int(
    os.environ.get("AI_TIMEOUT", "7200")
)  # hard wall-clock cap per invocation. Safety net for runaway processes
#  and the only timeout applied to the non-streaming path where we have no
#  intermediate visibility. Default 2 h.
CLAIM_TIMEOUT_SECONDS = int(
    os.environ.get("CLAIM_TIMEOUT_SECONDS", "600")
)  # stale-claim reset timeout. Previously 300s; raised to reduce the window
#  in which a slow delivery watcher plus a reset can cause token-mismatch
#  double-dispatch (see review H2).
MAX_REMINDER_ATTEMPTS = 10  # max delivery attempts before marking a reminder failed
REMINDER_MAX_AGE_SECONDS = int(
    os.environ.get("REMINDER_MAX_AGE_SECONDS", "604800")
)  # reject reminders whose fire_at is older than this (default: 7 days).
#  A bot offline for 2–3 days used to drop legitimate reminders at 24 h.

# ── System Prompts ──

ROBYX_SYSTEM_PROMPT = _load_prompt("prompt_orchestrator.md")
WORKSPACE_AGENT_SYSTEM_PROMPT = _load_prompt("prompt_workspace_agent.md")
FOCUSED_AGENT_SYSTEM_PROMPT = _load_prompt("prompt_focused_agent.md")
COLLABORATIVE_AGENT_SYSTEM_PROMPT = _load_prompt("prompt_collaborative_agent.md")