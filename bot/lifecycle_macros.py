"""Robyx — Lifecycle macros for scheduled & continuous tasks (spec 005, US2).

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
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

log = logging.getLogger("robyx.lifecycle_macros")


# ── Macro grammar ────────────────────────────────────────────────────────────


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


# ── Icon map (mirrors scheduled_delivery.TASK_TYPE_ICONS) ───────────────────


_ICONS: dict[str, str] = {
    "continuous": "🔄",
    "periodic": "⏰",
    "one-shot": "📌",
    "oneshot": "📌",
    "one_shot": "📌",
    "reminder": "🔔",
}


def _icon_for(task_type: str) -> str:
    return _ICONS.get((task_type or "").lower().strip(), "•")


# ── Data types ──────────────────────────────────────────────────────────────


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
    # Injection seams for tests — bypass the scheduler / continuous
    # modules with in-memory doubles.
    queue_reader: Any = None
    state_reader: Any = None


# ── Detection ────────────────────────────────────────────────────────────────


def parse_lifecycle_macros(text: str) -> list[MacroInvocation]:
    """Return every lifecycle macro in ``text`` in source order."""
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


# ── Workspace scoping ───────────────────────────────────────────────────────


def scope_to_workspace(
    entries: list[dict],
    chat_id: Any,
    thread_id: Any,
) -> list[dict]:
    """Filter queue entries to those belonging to the invoking workspace."""
    normalized = _normalize_thread_id(thread_id)
    return [
        e for e in (entries or [])
        if _normalize_thread_id(e.get("thread_id")) == normalized
    ]


def _normalize_thread_id(raw: Any) -> Any:
    if raw in (None, "", "-"):
        return None
    if isinstance(raw, str) and raw.lstrip("-").isdigit():
        return int(raw)
    return raw


# ── Task discovery & filtering ──────────────────────────────────────────────


def _is_active_status(status: str) -> bool:
    return status in {"pending", "running", "paused", "awaiting-input", "rate-limited"}


def _load_scoped_entries(ctx: DispatchContext) -> list[dict]:
    """Read the queue (via ctx.queue_reader or the real scheduler) and
    filter to the invoking workspace.
    """
    if ctx.queue_reader is not None:
        entries = list(ctx.queue_reader())
    else:
        from scheduler import load_queue
        entries = load_queue()
    return scope_to_workspace(entries, ctx.chat_id, ctx.thread_id)


def _load_continuous_state(entry: dict, ctx: DispatchContext) -> dict | None:
    """Resolve the state.json for a continuous queue entry."""
    if entry.get("type") != "continuous":
        return None
    if ctx.state_reader is not None:
        return ctx.state_reader(entry.get("name", ""))
    from pathlib import Path as _Path
    state_file = entry.get("state_file")
    if not state_file:
        from continuous import state_file_path
        path = state_file_path(entry.get("name", ""))
    else:
        path = _Path(state_file)
    from continuous import load_state
    return load_state(path)


def _effective_status(entry: dict, state: dict | None) -> str:
    """Prefer the continuous state's status over the queue entry's status."""
    if state is not None:
        s = state.get("status")
        if s:
            return s
    return entry.get("status", "pending") or "pending"


def _list_active_tasks(ctx: DispatchContext) -> list[dict]:
    """Return an enriched list of active tasks for the invoking workspace.

    Each element is ``{"entry": queue_entry, "state": continuous_state_or_None,
    "status": effective_status}``. Filters out completed / canceled /
    dispatched entries.
    """
    out: list[dict] = []
    for entry in _load_scoped_entries(ctx):
        state = _load_continuous_state(entry, ctx)
        status = _effective_status(entry, state)
        if not _is_active_status(status):
            continue
        out.append({"entry": entry, "state": state, "status": status})
    return out


def _match_by_name(tasks: list[dict], query: str) -> list[dict]:
    """Case-insensitive substring match on ``name`` over the candidate set."""
    if not query:
        return []
    needle = query.strip().lower()
    matches: list[dict] = []
    for t in tasks:
        name = (t["entry"].get("name") or "").lower()
        if not name:
            continue
        if needle == name or needle in name:
            matches.append(t)
    # Prefer exact-name matches first so "stop foo" with ["foo", "foobar"]
    # resolves to "foo" unambiguously.
    exact = [t for t in matches if (t["entry"].get("name") or "").lower() == needle]
    if exact:
        return exact
    return matches


# ── Renderers ────────────────────────────────────────────────────────────────


_TYPE_ORDER = ("continuous", "periodic", "one-shot", "reminder")
_TYPE_LABEL = {
    "continuous": "Continuous",
    "periodic": "Periodic",
    "one-shot": "One-shot",
    "reminder": "Reminder",
}


def _type_key(entry: dict) -> str:
    t = (entry.get("type") or "").lower().strip()
    if t in ("oneshot", "one_shot"):
        return "one-shot"
    return t or "one-shot"


def render_list(tasks: list[dict]) -> str:
    if not tasks:
        return "Nessun task attivo nel workspace."

    by_type: dict[str, list[dict]] = {t: [] for t in _TYPE_ORDER}
    for t in tasks:
        tk = _type_key(t["entry"])
        by_type.setdefault(tk, []).append(t)

    lines: list[str] = ["*Task attivi nel workspace* (%d)" % len(tasks), ""]
    for tk in _TYPE_ORDER:
        group = by_type.get(tk) or []
        if not group:
            continue
        icon = _icon_for(tk)
        lines.append("%s *%s*" % (icon, _TYPE_LABEL.get(tk, tk)))
        for t in group:
            lines.append("- %s" % _list_entry_line(t))
        lines.append("")
    return "\n".join(lines).rstrip()


def _list_entry_line(t: dict) -> str:
    entry = t["entry"]
    name = entry.get("name") or entry.get("id") or "?"
    status = t["status"]
    tk = _type_key(entry)
    extra = ""
    if tk == "continuous" and t["state"] is not None:
        obj = (t["state"].get("program") or {}).get("objective") or ""
        if obj:
            extra = " · obj: “%s”" % _shorten(obj, 60)
    elif tk == "periodic":
        nxt = entry.get("next_run")
        if nxt:
            extra = " · next: %s" % nxt
    elif tk == "one-shot":
        sched = entry.get("scheduled_at")
        if sched:
            extra = " · at %s" % sched
    elif tk == "reminder":
        fire = entry.get("fire_at")
        if fire:
            extra = " · fires %s" % fire
    return "`%s` — %s%s" % (name, status, extra)


def _shorten(s: str, max_chars: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"


def render_status(t: dict) -> str:
    entry = t["entry"]
    state = t["state"]
    tk = _type_key(entry)
    icon = _icon_for(tk)
    name = entry.get("name") or "?"
    lines = ["%s *%s* (`%s`)" % (icon, _TYPE_LABEL.get(tk, tk), name)]
    lines.append("Status: %s" % t["status"])
    if state is not None:
        program = state.get("program") or {}
        obj = program.get("objective") or ""
        if obj:
            lines.append("Objective: %s" % _shorten(obj, 200))
        total = state.get("total_steps_completed")
        if total is not None:
            lines.append("Steps completed: %d" % total)
        constraints = program.get("constraints") or []
        if constraints:
            lines.append("Constraints: " + ", ".join(str(c) for c in constraints[:5]))
        hist = state.get("history") or []
        if hist:
            last = hist[-1]
            desc = _shorten(str(last.get("description", "")), 120)
            lines.append("Last step: %s" % desc)
    else:
        # Non-continuous types — surface schedule / fire_at
        if tk == "periodic":
            nxt = entry.get("next_run")
            if nxt:
                lines.append("Next run: %s" % nxt)
        elif tk == "one-shot":
            sched = entry.get("scheduled_at")
            if sched:
                lines.append("Scheduled at: %s" % sched)
        elif tk == "reminder":
            fire = entry.get("fire_at")
            if fire:
                lines.append("Fires at: %s" % fire)
    return "\n".join(lines)


def render_ambiguous_candidates(matches: list[dict], query: str) -> str:
    lines = [
        'Ho trovato più task che corrispondono a "%s":' % query,
        "",
    ]
    for i, t in enumerate(matches, start=1):
        entry = t["entry"]
        tk = _type_key(entry)
        icon = _icon_for(tk)
        name = entry.get("name") or "?"
        lines.append(
            "%d. %s `%s` (%s, %s)"
            % (i, icon, name, _TYPE_LABEL.get(tk, tk).lower(), t["status"])
        )
    lines.append("")
    lines.append("Quale intendi?")
    return "\n".join(lines)


def render_not_found(query: str) -> str:
    return "Nessun task attivo chiamato `%s` nel workspace." % (query or "?")


# ── State mutators ──────────────────────────────────────────────────────────


def _log_action(
    ctx: DispatchContext,
    macro: str,
    name: str | None,
    resolved_to: str | None,
    outcome: str,
) -> None:
    log.info(
        "lifecycle.action ts=%s workspace_thread=%s macro=%s name=%r "
        "resolved_to=%r outcome=%s",
        datetime.now(timezone.utc).isoformat(),
        ctx.thread_id, macro, name, resolved_to, outcome,
    )


def _stop_task(t: dict, ctx: DispatchContext) -> str:
    entry = t["entry"]
    name = entry.get("name") or "?"
    tk = _type_key(entry)
    if tk == "continuous" and t["state"] is not None:
        from continuous import complete_task, save_state, state_file_path
        state = complete_task(t["state"])
        save_state(state_file_path(name), state)
        # Also mark the queue entry as canceled so the scheduler never
        # re-picks it (belt + suspenders on top of the status=completed).
        try:
            from scheduler import cancel_task_by_name
            cancel_task_by_name(name, reason="stopped by user")
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("stop_task: cancel_task_by_name failed for %s: %s", name, exc)
    else:
        # Periodic / one-shot: cancel the queue entry.
        from scheduler import cancel_task_by_name
        cancel_task_by_name(name, reason="stopped by user")
    _log_action(ctx, "stop_task", name, name, "stopped")
    return "Task `%s` fermato." % name


def _pause_task(t: dict, ctx: DispatchContext) -> str:
    entry = t["entry"]
    name = entry.get("name") or "?"
    tk = _type_key(entry)
    if tk != "continuous" or t["state"] is None:
        _log_action(ctx, "pause_task", name, name, "unsupported")
        return (
            "Pausa non supportata per task di tipo `%s`. "
            "Usa `ferma %s` per fermarlo definitivamente."
            % (tk, name)
        )
    from continuous import pause_task, save_state, state_file_path
    state = pause_task(t["state"])
    save_state(state_file_path(name), state)
    _log_action(ctx, "pause_task", name, name, "paused")
    return (
        "Task `%s` in pausa. Riprendi con: `ripristina %s`." % (name, name)
    )


def _resume_task(t: dict, ctx: DispatchContext) -> str:
    entry = t["entry"]
    name = entry.get("name") or "?"
    tk = _type_key(entry)
    if tk != "continuous" or t["state"] is None:
        _log_action(ctx, "resume_task", name, name, "unsupported")
        return (
            "Resume non supportato per task di tipo `%s`." % tk
        )
    state = t["state"]
    if state.get("status") not in ("paused", "rate-limited"):
        _log_action(ctx, "resume_task", name, name, "noop")
        return (
            "Task `%s` non è in pausa (status: %s)." % (name, state.get("status"))
        )
    from continuous import resume_task, save_state, state_file_path
    state = resume_task(state)
    save_state(state_file_path(name), state)
    _log_action(ctx, "resume_task", name, name, "resumed")
    return "Task `%s` ripreso." % name


def _get_plan(t: dict, ctx: DispatchContext) -> str:
    entry = t["entry"]
    name = entry.get("name") or "?"
    tk = _type_key(entry)
    if tk != "continuous":
        _log_action(ctx, "get_plan", name, name, "unsupported")
        return "Il piano dettagliato è disponibile solo per task continuativi."
    from continuous import read_plan_md
    content = read_plan_md(name)
    if content is None:
        _log_action(ctx, "get_plan", name, name, "missing")
        return "Piano non disponibile per `%s` (file mancante)." % name
    _log_action(ctx, "get_plan", name, name, "read")
    # Truncate very long plans to ~2000 chars per contract.
    if len(content) > 2000:
        content = content[:2000].rstrip() + "\n\n_[…troncato]_"
    return content


# ── Macro handlers ───────────────────────────────────────────────────────────


async def _handle_list_tasks(
    inv: MacroInvocation, ctx: DispatchContext,
) -> str:
    tasks = _list_active_tasks(ctx)
    _log_action(ctx, "list_tasks", None, None, "listed:%d" % len(tasks))
    return render_list(tasks)


async def _handle_task_status(
    inv: MacroInvocation, ctx: DispatchContext,
) -> str:
    query = inv.name or ""
    matches = _match_by_name(_list_active_tasks(ctx), query)
    if not matches:
        _log_action(ctx, "task_status", query, None, "not_found")
        return render_not_found(query)
    if len(matches) == 1:
        _log_action(ctx, "task_status", query, matches[0]["entry"].get("name"), "rendered")
        return render_status(matches[0])
    _log_action(ctx, "task_status", query, None, "ambiguous:%d" % len(matches))
    return render_ambiguous_candidates(matches, query)


async def _handle_mutating(
    inv: MacroInvocation, ctx: DispatchContext, *,
    macro: str, action,
) -> str:
    query = inv.name or ""
    matches = _match_by_name(_list_active_tasks(ctx), query)
    if not matches:
        _log_action(ctx, macro, query, None, "not_found")
        return render_not_found(query)
    if len(matches) > 1:
        _log_action(ctx, macro, query, None, "ambiguous:%d" % len(matches))
        return render_ambiguous_candidates(matches, query)
    return action(matches[0], ctx)


async def _handle_stop_task(
    inv: MacroInvocation, ctx: DispatchContext,
) -> str:
    return await _handle_mutating(inv, ctx, macro="stop_task", action=_stop_task)


async def _handle_pause_task(
    inv: MacroInvocation, ctx: DispatchContext,
) -> str:
    return await _handle_mutating(inv, ctx, macro="pause_task", action=_pause_task)


async def _handle_resume_task(
    inv: MacroInvocation, ctx: DispatchContext,
) -> str:
    return await _handle_mutating(inv, ctx, macro="resume_task", action=_resume_task)


async def _handle_get_plan(
    inv: MacroInvocation, ctx: DispatchContext,
) -> str:
    return await _handle_mutating(inv, ctx, macro="get_plan", action=_get_plan)


_HANDLERS = {
    "list_tasks": _handle_list_tasks,
    "task_status": _handle_task_status,
    "stop_task": _handle_stop_task,
    "pause_task": _handle_pause_task,
    "resume_task": _handle_resume_task,
    "get_plan": _handle_get_plan,
}


# ── Public dispatch entry point ─────────────────────────────────────────────


async def handle_lifecycle_macros(
    invocations: list[MacroInvocation],
    ctx: DispatchContext,
) -> dict[tuple[int, int], str]:
    """Resolve each invocation against authoritative state and render the
    substitution string for it.

    Returns a mapping ``{span: rendered_markdown}`` so the caller can splice
    substitutions back into the primary agent's response in reverse order
    (earlier offsets stay valid during replacement).
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
        except Exception as exc:
            log.error(
                "lifecycle_macros: dispatch failed kind=%s name=%r exc=%s",
                inv.kind, inv.name, exc, exc_info=True,
            )
            substitutions[inv.span] = (
                "Errore nell'elaborare il comando `%s`. Nessuna modifica applicata."
                % inv.kind
            )
    return substitutions


def substitute_macros(text: str, substitutions: dict[tuple[int, int], str]) -> str:
    """Apply span→string substitutions to ``text`` in reverse offset order."""
    if not substitutions:
        return text
    out = text
    for (start, end), replacement in sorted(
        substitutions.items(), key=lambda kv: kv[0][0], reverse=True,
    ):
        out = out[:start] + (replacement or "") + out[end:]
    # Collapse runs of ≥3 newlines produced by substitutions back to 2.
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


__all__ = [
    "MacroKind",
    "MacroInvocation",
    "DispatchContext",
    "parse_lifecycle_macros",
    "handle_lifecycle_macros",
    "substitute_macros",
    "scope_to_workspace",
    "render_list",
    "render_status",
    "render_ambiguous_candidates",
    "render_not_found",
]
