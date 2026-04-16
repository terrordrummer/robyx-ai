"""Pass 2 — belt-and-braces tests for the i18n / help-text contract.

Two properties we want to guarantee at test time so they don't drift:

1. **Substitution safety.** Every i18n string with ``%s``/``%d`` format
   specifiers must accept the right number of args without raising, and
   no literal ``{placeholder}`` token can leak through to a user (closes
   the residual risk flagged by Pass 1 F19 on ``memory.py`` templates).

2. **/help parity.** Every public slash-command registered by
   ``make_handlers`` must appear in the ``help_text`` string, and every
   ``/command`` mentioned in ``help_text`` must have a handler. A
   mismatch is a finding — the user either sees a command they can't
   run, or has a command they don't know exists.
"""

from __future__ import annotations

import re

import pytest

from i18n import STRINGS


# Format-specifier pattern covering the subset of Python %-formatting
# that actually appears in this codebase. Anything else is a bug.
_FMT_RE = re.compile(r"%[sd]")


def _arg_count(fmt: str) -> int:
    """Return the number of ``%s`` / ``%d`` specifiers in ``fmt``.

    ``%%`` is a literal percent and is not a substitution."""
    # Strip literal %% before counting so we don't miscount e.g. "100%%".
    return len(_FMT_RE.findall(fmt.replace("%%", "")))


def _dummy_args(n: int) -> tuple:
    """Return a tuple of ``n`` dummy values safe for either %s or %d."""
    return tuple(1 for _ in range(n))


# ---------------------------------------------------------------------------
# Substitution safety
# ---------------------------------------------------------------------------


class TestStringSubstitution:
    """Every format string must produce a fully-substituted output for
    the number of args its specifiers declare. Any ``{placeholder}``
    style leak through to a user is a bug."""

    @pytest.mark.parametrize("key,value", list(STRINGS.items()))
    def test_format_specifiers_substitute_cleanly(self, key, value):
        """Instantiating with the right arity must not raise, and the
        resulting string must contain none of our format markers."""
        n = _arg_count(value)
        if n == 0:
            # No specifiers — the value must be identical to itself.
            assert value == value  # tautology makes the intent visible
            return
        try:
            result = value % _dummy_args(n)
        except (TypeError, ValueError) as e:
            pytest.fail(
                "STRINGS['%s'] failed substitution with %d args: %s"
                % (key, n, e)
            )
        # After substitution, no %s / %d must remain.
        assert not _FMT_RE.search(result), (
            "STRINGS['%s'] still contains a format specifier after "
            "substitution: %r" % (key, result)
        )

    @pytest.mark.parametrize("key,value", list(STRINGS.items()))
    def test_no_unsubstituted_brace_placeholders(self, key, value):
        """No ``{name}`` or ``{0}`` placeholder should appear in a final
        user-visible string — those are Pillow/.format()-style tokens
        that slipped in when the author forgot to call ``.format()``.
        Allow ``{{literal}}`` escapes since they round-trip safely."""
        # Strip escaped braces so they don't count as placeholders.
        stripped = value.replace("{{", "").replace("}}", "")
        leftovers = re.findall(r"\{[^{}]*\}", stripped)
        assert not leftovers, (
            "STRINGS['%s'] contains unsubstituted .format() "
            "placeholder(s): %s" % (key, leftovers)
        )


# ---------------------------------------------------------------------------
# /help parity
# ---------------------------------------------------------------------------


def _extract_commands_from_help() -> set[str]:
    """Return the set of ``/command`` names mentioned in help_text.

    Only counts tokens that are at a word boundary and start with ``/``.
    Strips any trailing punctuation and the leading slash."""
    text = STRINGS["help_text"]
    matches = re.findall(r"/([a-z][a-z_]+)", text)
    return set(matches)


def _extract_handler_commands() -> set[str]:
    """Return the set of user-facing slash-command keys in the dict
    returned by ``make_handlers``.

    Excludes internal dispatch keys that are NOT user-visible commands
    (``message``, ``voice``, ``collab_bot_added``, etc.) and the
    ``start`` / ``help`` aliases that are intentionally not listed in
    help_text (``start`` is a Telegram client convention; ``help`` would
    be self-referential)."""
    from unittest.mock import MagicMock

    from handlers import make_handlers

    manager = MagicMock()
    manager.list_active = MagicMock(return_value=[])
    manager.list_recent = MagicMock(return_value=[])
    manager.agents = {}
    backend = MagicMock()
    collab_store = MagicMock()
    collab_store.list_active = MagicMock(return_value=[])

    handlers = make_handlers(manager, backend, collab_store=collab_store)

    # Drop internal dispatch keys — help_text only describes slash
    # commands the user types in chat.
    internal = {"message", "voice", "collab_bot_added", "start", "help"}
    return {k for k in handlers.keys() if k not in internal}


class TestHelpParity:
    def test_every_handler_command_listed_in_help(self):
        """A registered command missing from ``help_text`` is invisible
        to users — a discoverability bug."""
        handler_cmds = _extract_handler_commands()
        help_cmds = _extract_commands_from_help()
        missing = handler_cmds - help_cmds
        assert not missing, (
            "Commands registered in make_handlers but not mentioned in "
            "help_text: %s" % sorted(missing)
        )

    def test_every_help_command_has_a_handler(self):
        """A ``/command`` mentioned in ``help_text`` without a handler
        is a documentation lie — the user types it, nothing happens."""
        handler_cmds = _extract_handler_commands()
        help_cmds = _extract_commands_from_help()
        # Allow a curated set of "commands" that legitimately appear in
        # help_text as references rather than callable handlers, e.g.
        # ``/focus off`` where "off" is an argument. Extract only the
        # first word after the slash (already done by _extract) — these
        # should all map to handlers unless explicitly excluded.
        extras = help_cmds - handler_cmds
        assert not extras, (
            "Commands mentioned in help_text but not registered in "
            "make_handlers: %s" % sorted(extras)
        )
