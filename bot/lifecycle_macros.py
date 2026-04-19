"""Robyx — Lifecycle macros for scheduled & continuous tasks (spec 005).

Owns detection, parsing, and workspace-scoped dispatch of lifecycle macros
emitted by the primary workspace agent:

  ``[LIST_TASKS]``
  ``[TASK_STATUS name="…"]``
  ``[STOP_TASK name="…"]``
  ``[PAUSE_TASK name="…"]``
  ``[RESUME_TASK name="…"]``
  ``[GET_PLAN name="…"]``

The primary agent recognises natural-language lifecycle intents ("lista task",
"ferma daily-report") and emits the corresponding macro. This module parses
the response, filters authoritative state (``data/queue.json`` +
``data/continuous/*/state.json``) by the invoking workspace, applies the
mutation atomically (for STOP/PAUSE/RESUME), and returns a rendered markdown
body that substitutes the macro before the response reaches the user.

Contract: ``specs/005-unified-workspace-chat/contracts/lifecycle-macros.md``.

This module is the **skeleton** produced by T006 (Phase 2 foundational). The
individual macro handlers are filled in by US2 tasks T027–T033. Calling an
unimplemented macro before US2 lands raises ``NotImplementedError``; the
caller (``handlers.py`` wiring in T034) is expected to gate invocation
behind a feature-ready check until then.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

log = logging.getLogger("robyx.lifecycle_macros")


# ── Macro grammar ────────────────────────────────────────────────────────────


# Attribute-value delimiter class mirrors ``continuous_macro._QUOTE`` — accepts
# ASCII double quotes and curly doubles/singles so tokenisers with mixed
# quoting styles still match.
_QUOTE = r'["\u201C\u201D\u2018\u2019]'
_NAME_ATTR = (
    r"name\s*=\s*" + _QUOTE + r"([^\"\u201C\u201D\u2018\u2019]+)" + _QUOTE
)

_LIST_TASKS_RE = re.compile(r"\[\s*LIST_TASKS\s*\]", re.IGNORECASE)
_TASK_STATUS_RE = re.compile(
    r"\[\s*TASK_STATUS\s+" + _NAME_ATTR + r"\s*\]",
    re.IGNORECASE,
)
_STOP_TASK_RE = re.compile(
    r"\[\s*STOP_TASK\s+" + _NAME_ATTR + r"\s*\]",
    re.IGNORECASE,
)
_PAUSE_TASK_RE = re.compile(
    r"\[\s*PAUSE_TASK\s+" + _NAME_ATTR + r"\s*\]",
    re.IGNORECASE,
)
_RESUME_TASK_RE = re.compile(
    r"\[\s*RESUME_TASK\s+" + _NAME_ATTR + r"\s*\]",
    re.IGNORECASE,
)
_GET_PLAN_RE = re.compile(
    r"\[\s*GET_PLAN\s+" + _NAME_ATTR + r"\s*\]",
    re.IGNORECASE,
)


MacroKind = Literal[
    "list_tasks",
    "task_status",
    "stop_task",
    "pause_task",
    "resume_task",
    "get_plan",
]


@dataclass
class MacroInvocation:
    """One detected lifecycle macro with its location in source text."""

    kind: MacroKind
    name: str | None
    span: tuple[int, int]


@dataclass
class DispatchContext:
    """Parameters threaded into ``handle_lifecycle_macros``.

    Scopes all lookups to the invoking workspace — ``chat_id`` and
    ``thread_id`` identify the parent workspace chat; tasks belonging to
    other workspaces are invisible to these macros.
    """

    chat_id: Any
    thread_id: Any
    platform: Any = None
    manager: Any = None
    # Injection seams for tests.
    queue_reader: Any = None
    state_reader: Any = None


# ── Detection ────────────────────────────────────────────────────────────────


def parse_lifecycle_macros(text: str) -> list[MacroInvocation]:
    """Return every lifecycle macro in ``text`` in source order.

    Pure function: no I/O, no side effects, no ``json.loads``. Malformed
    macros are ignored (no bracketed token that doesn't match any of the
    six patterns is surfaced here — those are stripped by the caller's
    control-token scrub or logged at WARN in the dispatcher).
    """
    if not text:
        return []

    hits: list[MacroInvocation] = []
    for m in _LIST_TASKS_RE.finditer(text):
        hits.append(
            MacroInvocation(kind="list_tasks", name=None, span=(m.start(), m.end()))
        )
    for kind, pattern in (
        ("task_status", _TASK_STATUS_RE),
        ("stop_task", _STOP_TASK_RE),
        ("pause_task", _PAUSE_TASK_RE),
        ("resume_task", _RESUME_TASK_RE),
        ("get_plan", _GET_PLAN_RE),
    ):
        for m in pattern.finditer(text):
            hits.append(
                MacroInvocation(
                    kind=kind,  # type: ignore[arg-type]
                    name=m.group(1).strip(),
                    span=(m.start(), m.end()),
                )
            )
    hits.sort(key=lambda h: h.span[0])
    return hits


# ── Dispatch ─────────────────────────────────────────────────────────────────


async def handle_lifecycle_macros(
    invocations: list[MacroInvocation],
    ctx: DispatchContext,
) -> dict[tuple[int, int], str]:
    """Resolve each invocation against authoritative state and render a
    substitution string for it.

    Returns a mapping ``{span: rendered_markdown}`` so the caller can splice
    substitutions back into the primary agent's response in reverse order
    (earlier offsets stay valid during replacement).

    **Skeleton behaviour (T006)**: recognised macros return
    ``NotImplementedError`` sentinel strings so the wiring site can detect
    the feature-not-ready state without crashing. US2 tasks fill this in.
    """
    if not invocations:
        return {}
    substitutions: dict[tuple[int, int], str] = {}
    for inv in invocations:
        handler = _HANDLERS.get(inv.kind)
        if handler is None:
            log.warning("lifecycle_macros: unknown kind=%r — stripping", inv.kind)
            substitutions[inv.span] = ""
            continue
        try:
            substitutions[inv.span] = await handler(inv, ctx)
        except NotImplementedError:
            # Feature gate: skeleton returns an empty substitution so the
            # macro is stripped from user-visible output until US2 lands.
            log.info(
                "lifecycle_macros: handler for %r not yet implemented — stripping",
                inv.kind,
            )
            substitutions[inv.span] = ""
        except Exception as exc:
            log.error(
                "lifecycle_macros: dispatch failed kind=%s name=%r exc=%s",
                inv.kind, inv.name, exc, exc_info=True,
            )
            substitutions[inv.span] = ""
    return substitutions


# ── Scoping helper (used by every handler in US2) ───────────────────────────


def scope_to_workspace(
    entries: list[dict],
    chat_id: Any,
    thread_id: Any,
) -> list[dict]:
    """Filter queue entries to those owned by the invoking workspace.

    A queue entry is considered owned by ``(chat_id, thread_id)`` when the
    entry's ``chat_id`` matches and the entry's ``thread_id`` either matches
    (reminders, periodic/one-shot tasks persisted with the workspace's
    thread) or the entry's ``workspace_thread_id`` matches (continuous
    tasks whose state references the parent workspace post-0.23.0).

    Legacy continuous entries that still point at their old sub-topic
    ``thread_id`` remain visible to the workspace that owns them because
    ``workspace_thread_id`` in their state file continues to record the
    parent. Post-migration, both point at the same parent thread.
    """
    scoped: list[dict] = []
    for entry in entries or []:
        if _entry_chat_id(entry) != chat_id:
            continue
        if (
            _entry_thread_id(entry) == thread_id
            or _entry_workspace_thread_id(entry) == thread_id
        ):
            scoped.append(entry)
    return scoped


def _entry_chat_id(entry: dict) -> Any:
    raw = entry.get("chat_id")
    if isinstance(raw, str) and raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _entry_thread_id(entry: dict) -> Any:
    raw = entry.get("thread_id")
    if raw in (None, "", "-"):
        return None
    if isinstance(raw, str) and raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _entry_workspace_thread_id(entry: dict) -> Any:
    raw = entry.get("workspace_thread_id")
    if raw in (None, "", "-"):
        return None
    if isinstance(raw, str) and raw.lstrip("-").isdigit():
        return int(raw)
    return raw


# ── Handler table (skeleton) ────────────────────────────────────────────────


async def _handle_not_implemented(
    inv: MacroInvocation, ctx: DispatchContext,
) -> str:  # pragma: no cover - replaced in US2
    raise NotImplementedError(
        "lifecycle macro handler %r pending US2 implementation" % inv.kind
    )


# Populated here by T027–T033. Until then every handler is the skeleton stub.
_HANDLERS: dict[MacroKind, Any] = {
    "list_tasks": _handle_not_implemented,
    "task_status": _handle_not_implemented,
    "stop_task": _handle_not_implemented,
    "pause_task": _handle_not_implemented,
    "resume_task": _handle_not_implemented,
    "get_plan": _handle_not_implemented,
}


__all__ = [
    "MacroKind",
    "MacroInvocation",
    "DispatchContext",
    "parse_lifecycle_macros",
    "handle_lifecycle_macros",
    "scope_to_workspace",
]
