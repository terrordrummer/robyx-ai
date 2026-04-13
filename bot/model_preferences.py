"""Robyx — Backend-aware model preference resolution.

Workspaces, specialists, and scheduled tasks can express their model
preference as a semantic alias (``fast``, ``balanced``, ``powerful``) or as
a role (``orchestrator``, ``workspace``, ``specialist``, ``scheduled``,
``one-shot``). At invocation time Robyx resolves the preference into the
concrete model id that the active backend understands, using the alias
table loaded from ``models.yaml`` (see :mod:`config`).

Legacy Claude-style names (``haiku``/``sonnet``/``opus``) keep working: they
are silently mapped onto ``fast``/``balanced``/``powerful``, so existing
``tasks.md`` and ``specialists.md`` rows continue to load without
modification.
"""

from config import AI_BACKEND, AI_MODEL_ALIASES, AI_MODEL_DEFAULTS


# Legacy Claude-style aliases that some agents/tasks may still reference.
# Mapped to the semantic aliases used by ``models.yaml``.
LEGACY_MODEL_ALIASES = {
    "haiku": "fast",
    "sonnet": "balanced",
    "opus": "powerful",
}


def get_backend_key(backend) -> str:
    """Normalise a backend instance, class, or name into a config key.

    The key is the lowercase identifier used in ``models.yaml`` to look up
    the per-backend model id (``claude``, ``codex``, ``opencode``).
    """
    if isinstance(backend, str):
        return backend.lower()

    key = getattr(backend, "key", None)
    if isinstance(key, str) and key:
        return key.lower()

    name_attr = getattr(backend, "name", None)
    if isinstance(name_attr, str) and name_attr:
        first_word = name_attr.split()[0].strip().lower()
        if first_word in ("claude", "codex", "opencode"):
            return first_word

    name = backend.__class__.__name__.lower()
    if name.endswith("backend"):
        name = name[:-7]
    return name


def get_default_model_preference(role: str | None = None) -> str:
    """Return the configured default alias for an agent role.

    Falls back to the ``workspace`` default and finally to ``balanced`` so a
    misconfigured ``models.yaml`` never crashes the bot.
    """
    role_key = (role or "workspace").strip().lower()
    defaults = AI_MODEL_DEFAULTS if isinstance(AI_MODEL_DEFAULTS, dict) else {}
    return defaults.get(role_key) or defaults.get("workspace") or "balanced"


def resolve_model_preference(
    model: str | None,
    backend,
    role: str | None = None,
) -> str:
    """Resolve an alias / role / explicit model into a backend-specific model id.

    Resolution order:

    1. If *model* is explicitly set, use it (after legacy alias mapping).
    2. Otherwise, fall back to the configured default for *role*.
    3. Look the result up in ``aliases[<alias>][<backend>]``. If found,
       return the concrete model id; otherwise return the alias itself —
       this lets users put a literal model id (e.g. ``"openai/gpt-5"``) in
       the agent definition and have Robyx pass it through unchanged.
    """
    backend_key = get_backend_key(backend)
    preferred = (model or "").strip() or get_default_model_preference(role)
    alias_key = LEGACY_MODEL_ALIASES.get(preferred, preferred)

    aliases = AI_MODEL_ALIASES if isinstance(AI_MODEL_ALIASES, dict) else {}
    alias_value = aliases.get(alias_key)
    if isinstance(alias_value, dict):
        resolved = alias_value.get(backend_key) or alias_value.get(AI_BACKEND)
        if isinstance(resolved, str) and resolved.strip():
            return resolved.strip()

    return preferred
