"""Robyx — ``[UPDATE_PLAN]`` macro for in-place continuous-task edits.

Lets the primary workspace agent modify an existing continuous task's
program (objective, success_criteria, constraints, checkpoint_policy,
context, plan_text) without creating a new task. Grammar:

    [UPDATE_PLAN name="<slug>"]
    [CONTINUOUS_PROGRAM]
    { ...partial program JSON... }
    [/CONTINUOUS_PROGRAM]

All fields are optional — omitted keys are preserved as-is. The macro is
workspace-scoped: tasks whose ``workspace_thread_id`` does not match the
invoking thread are invisible (reported as "not found").

Detection is tolerant (case-insensitive tags, ASCII or curly quotes,
code fences). Stripping is unconditional — any partial or malformed
match is scrubbed so the failure mode is "not applied", never "leaked".

Structural failures (missing program block, bad JSON, unknown task)
produce a short prose line from ``i18n.STRINGS`` and log a warning; the
state file is never touched. Successful updates persist via
``continuous.save_state`` / ``continuous.write_plan_md`` with the same
atomic write-then-rename pattern used elsewhere.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from task_scope import (
    TaskScope,
    attach_scope,
    legacy_scope_matches,
    scope_from_record,
)

log = logging.getLogger("robyx.update_plan_macro")


# ── Reject reasons ──────────────────────────────────────────────────────────

REJECT_REASONS: tuple[str, ...] = (
    "malformed_missing_open",
    "malformed_missing_program",
    "malformed_unclosed_program",
    "bad_json",
    "bad_field",
    "not_found",
    "downstream_error",
)

RejectReason = Literal[
    "malformed_missing_open",
    "malformed_missing_program",
    "malformed_unclosed_program",
    "bad_json",
    "bad_field",
    "not_found",
    "downstream_error",
]


# ── Grammar ─────────────────────────────────────────────────────────────────


_QUOTE = r'["\u201C\u201D\u2018\u2019]'

_UPDATE_PLAN_RE = re.compile(
    r'\[\s*UPDATE_PLAN'
    r'\s+name\s*=\s*' + _QUOTE + r'([^"\u201C\u201D\u2018\u2019]+)' + _QUOTE +
    r'\s*\]',
    re.IGNORECASE | re.DOTALL,
)

_CONTINUOUS_PROGRAM_PAIR_RE = re.compile(
    r'\[\s*CONTINUOUS_PROGRAM\s*\](.*?)\[\s*/\s*CONTINUOUS_PROGRAM\s*\]',
    re.IGNORECASE | re.DOTALL,
)

_CONTINUOUS_PROGRAM_OPEN_RE = re.compile(
    r'\[\s*CONTINUOUS_PROGRAM\s*\]',
    re.IGNORECASE,
)

_FENCE_RE = re.compile(
    r'(?P<fence_open>```[a-zA-Z0-9_-]*\s*\n?)(?P<body>.*?)(?P<fence_close>\n?```)',
    re.DOTALL,
)

# Same 64 KiB cap as ``continuous_macro._MAX_PROGRAM_BYTES``.
_MAX_PROGRAM_BYTES = 64 * 1024


# ── Data types ──────────────────────────────────────────────────────────────


@dataclass
class UpdatePlanTokens:
    open_span: tuple[int, int] | None = None
    program_span: tuple[int, int] | None = None
    name_raw: str | None = None
    program_raw: str | None = None
    surrounding_fence: tuple[int, int] | None = None
    unclosed_program: bool = False


@dataclass
class UpdatePlanOutcome:
    outcome: Literal["applied", "rejected"]
    name: str = "?"
    reason: RejectReason | None = None
    detail: str | None = None


@dataclass
class UpdatePlanContext:
    thread_id: Any
    chat_id: Any = None
    task_scope: TaskScope | None = None
    manager: Any = None
    platform: Any = None
    # Injection seam for tests.
    state_reader: Any = None
    state_writer: Any = None
    plan_writer: Any = None


# ── Pure extraction ─────────────────────────────────────────────────────────


def extract_update_plan_macros(
    text: str,
) -> tuple[str, list[UpdatePlanTokens]]:
    """Detect and strip every UPDATE_PLAN macro in ``text``.

    Mirrors the structure of
    ``continuous_macro.extract_continuous_macros`` so both macros behave
    the same way under fences, unclosed program blocks, and mixed quotes.

    Pure function: no I/O, no logging, no ``json.loads``.
    """
    if not text:
        return text, []

    opens = list(_UPDATE_PLAN_RE.finditer(text))
    pairs = list(_CONTINUOUS_PROGRAM_PAIR_RE.finditer(text))

    # Stray (unclosed) CONTINUOUS_PROGRAM openers that are not part of a
    # paired block. Note: continuous_macro.py also scans for these — if
    # both modules ran on the same text, they would compete for the same
    # openers. In practice, UPDATE_PLAN runs AFTER apply_continuous_macros
    # has already stripped CREATE_CONTINUOUS / CONTINUOUS_PROGRAM pairs
    # that belong to it, so the remaining pairs here belong to us.
    stray_opens: list[re.Match[str]] = []
    for m in _CONTINUOUS_PROGRAM_OPEN_RE.finditer(text):
        inside_pair = any(p.start() <= m.start() < p.end() for p in pairs)
        if inside_pair:
            continue
        stray_opens.append(m)

    tokens: list[UpdatePlanTokens] = []
    used_pair_idx: set[int] = set()
    used_stray_idx: set[int] = set()

    for open_m in opens:
        tok = UpdatePlanTokens(
            open_span=(open_m.start(), open_m.end()),
            name_raw=open_m.group(1),
        )
        # Closest following paired program block.
        paired = None
        for idx, p in enumerate(pairs):
            if idx in used_pair_idx:
                continue
            if p.start() >= open_m.end():
                paired = (idx, p)
                break
        if paired is not None:
            idx, p = paired
            used_pair_idx.add(idx)
            tok.program_span = (p.start(), p.end())
            tok.program_raw = p.group(1)
        else:
            for idx, s in enumerate(stray_opens):
                if idx in used_stray_idx:
                    continue
                if s.start() >= open_m.end():
                    used_stray_idx.add(idx)
                    tok.program_span = (s.start(), len(text))
                    tok.program_raw = text[s.end():]
                    tok.unclosed_program = True
                    break
        tokens.append(tok)

    if not tokens:
        return text, []

    for tok in tokens:
        span = _primary_span(tok)
        if span is None:
            continue
        fence_span = _enclosing_fence(text, span)
        if fence_span is not None:
            tok.surrounding_fence = fence_span

    tokens.sort(key=lambda t: _primary_span(t)[0] if _primary_span(t) else 0)

    spans_to_remove: list[tuple[int, int]] = []
    for tok in tokens:
        if tok.surrounding_fence is not None:
            spans_to_remove.append(tok.surrounding_fence)
            continue
        if tok.open_span is not None:
            spans_to_remove.append(tok.open_span)
        if tok.program_span is not None:
            spans_to_remove.append(tok.program_span)
    spans_to_remove = _merge_spans(spans_to_remove)
    spans_to_remove.sort(reverse=True)
    stripped = text
    for start, end in spans_to_remove:
        stripped = stripped[:start] + stripped[end:]

    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped, tokens


def _primary_span(tok: UpdatePlanTokens) -> tuple[int, int] | None:
    if tok.open_span is not None and tok.program_span is not None:
        return (
            min(tok.open_span[0], tok.program_span[0]),
            max(tok.open_span[1], tok.program_span[1]),
        )
    return tok.open_span or tok.program_span


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    merged = [spans[0]]
    for s, e in spans[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def _enclosing_fence(
    text: str, inner_span: tuple[int, int],
) -> tuple[int, int] | None:
    for m in _FENCE_RE.finditer(text):
        body_start = m.start("body")
        body_end = m.end("body")
        if body_start > inner_span[0] or body_end < inner_span[1]:
            continue
        before = text[body_start:inner_span[0]]
        after = text[inner_span[1]:body_end]
        if before.strip() or after.strip():
            continue
        return (m.start(), m.end())
    return None


# ── Field validation ────────────────────────────────────────────────────────


_ALLOWED_POLICIES = frozenset({
    "on-demand", "on-uncertainty", "on-milestone", "every-N-steps",
})


def _validate_overrides(program: dict) -> tuple[dict, str | None]:
    """Return ``(normalized_overrides, bad_field_name_or_None)``.

    Accepts any subset of: ``objective`` (str), ``success_criteria``
    (list[str]), ``constraints`` (list[str]), ``checkpoint_policy`` (str
    from allowed set), ``context`` (str), ``plan_text`` (str).

    Unknown fields are ignored (forward-compatibility).
    """
    out: dict = {}

    if "objective" in program:
        v = program["objective"]
        if not isinstance(v, str) or not v.strip():
            return {}, "objective"
        out["objective"] = v.strip()

    if "success_criteria" in program:
        v = program["success_criteria"]
        if not isinstance(v, list) or not all(
            isinstance(x, str) and x.strip() for x in v
        ):
            return {}, "success_criteria"
        out["success_criteria"] = [x.strip() for x in v]

    if "constraints" in program:
        v = program["constraints"]
        if not isinstance(v, list) or not all(
            isinstance(x, str) for x in v
        ):
            return {}, "constraints"
        out["constraints"] = [x.strip() for x in v if x.strip()]

    if "checkpoint_policy" in program:
        v = program["checkpoint_policy"]
        if not isinstance(v, str) or v not in _ALLOWED_POLICIES:
            return {}, "checkpoint_policy"
        out["checkpoint_policy"] = v

    if "context" in program:
        v = program["context"]
        if not isinstance(v, str):
            return {}, "context"
        out["context"] = v

    if "plan_text" in program:
        v = program["plan_text"]
        if not isinstance(v, str):
            return {}, "plan_text"
        out["plan_text"] = v

    return out, None


# ── Plan.md renderer ────────────────────────────────────────────────────────


def _render_plan_md(name: str, program: dict) -> str:
    """Render a minimal ``plan.md`` from a program dict.

    Used when the UPDATE_PLAN payload does not supply a free-form
    ``plan_text``. Kept deliberately small: plan.md is for human review
    (and for the step agent's ``{{PLAN_MD}}`` block), not a contract.
    """
    parts = ["# Plan: %s" % name, ""]

    obj = (program.get("objective") or "").strip()
    if obj:
        parts.extend(["## Objective", "", obj, ""])

    criteria = program.get("success_criteria") or []
    if criteria:
        parts.append("## Success Criteria")
        parts.append("")
        parts.extend("- %s" % c for c in criteria)
        parts.append("")

    constraints = program.get("constraints") or []
    if constraints:
        parts.append("## Constraints")
        parts.append("")
        parts.extend("- %s" % c for c in constraints)
        parts.append("")

    policy = (program.get("checkpoint_policy") or "on-demand").strip()
    parts.extend(["## Checkpoint Policy", "", "`%s`" % policy, ""])

    ctx = (program.get("context") or "").strip()
    if ctx:
        parts.extend(["## Context", "", ctx, ""])

    return "\n".join(parts).rstrip() + "\n"


# ── Side-effect application ─────────────────────────────────────────────────


def _lazy_strings() -> dict:
    from i18n import STRINGS
    return STRINGS


def _find_task(name: str, ctx: UpdatePlanContext) -> tuple[Any, dict | None]:
    """Find a continuous task by name, scoped to the invoking workspace.

    Returns ``(state_file_path, state_dict_or_None)``. The path is
    returned even on miss so the caller can log it. State is ``None``
    when the task is missing, unreadable, or owned by a different
    workspace.
    """
    from pathlib import Path
    if ctx.state_reader is not None:
        path, state = ctx.state_reader(name)
    else:
        from continuous import load_state, state_file_path
        path = state_file_path(name)
        state = load_state(Path(path)) if path else None

    if state is None:
        return path, None

    # Modern records are authorized by the complete immutable scope.  Legacy
    # tests/callers without a boundary TaskScope retain the historical thread
    # check, while production legacy data must pass explicit migration
    # evidence before it becomes visible.
    task_thread = state.get("workspace_thread_id")
    if ctx.task_scope is not None:
        try:
            persisted = scope_from_record(state)
        except (TypeError, ValueError):
            log.error("update_plan invalid workspace_scope name=%s", name)
            return path, None
        if persisted is not None:
            if persisted != ctx.task_scope:
                return path, None
            return path, state
        if not legacy_scope_matches(
            state,
            ctx.task_scope,
            legacy_parent_thread_id=task_thread,
            manager=ctx.manager,
        ):
            return path, None
        attach_scope(state, ctx.task_scope)
        return path, state

    if _norm_thread(task_thread) != _norm_thread(ctx.thread_id):
        return path, None

    return path, state


def _norm_thread(raw: Any) -> Any:
    if raw in (None, "", "-"):
        return None
    if isinstance(raw, str) and raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _ensure_queue_scope(name: str, ctx: UpdatePlanContext) -> None:
    """Validate/migrate the queue half of a production canonical scope."""
    if ctx.task_scope is None or ctx.state_writer is not None:
        return
    from scheduler import load_queue, set_continuous_workspace_scope

    active = [
        entry for entry in load_queue()
        if entry.get("type") == "continuous"
        and entry.get("name") == name
        and entry.get("status") != "canceled"
    ]
    if len(active) != 1:
        raise RuntimeError(
            "continuous queue identity is ambiguous for task '%s'" % name
        )
    persisted = scope_from_record(active[0])
    if persisted is not None and persisted != ctx.task_scope:
        raise RuntimeError("continuous queue scope conflict for task '%s'" % name)
    if persisted is None:
        set_continuous_workspace_scope(name, ctx.task_scope.to_dict())


async def apply_update_plan_macros(
    response: str,
    ctx: UpdatePlanContext,
) -> tuple[str, list[UpdatePlanOutcome]]:
    """Extract, validate, and apply every UPDATE_PLAN macro in ``response``.

    Returns ``(user_visible_response, outcomes)``. The returned text has
    every macro stripped; one prose line from ``i18n.STRINGS`` is
    appended per outcome (confirmation or error).
    """
    stripped, tokens = extract_update_plan_macros(response)
    if not tokens:
        return response, []

    outcomes: list[UpdatePlanOutcome] = []
    strings = _lazy_strings()
    lines: list[str] = []

    for tok in tokens:
        name = (tok.name_raw or "?").strip() or "?"

        # Structural gates.
        if tok.open_span is None and tok.program_span is not None:
            outcomes.append(UpdatePlanOutcome(
                outcome="rejected", name=name,
                reason="malformed_missing_open",
            ))
            lines.append(strings["update_plan_error_malformed"])
            continue
        if tok.open_span is not None and tok.program_span is None:
            outcomes.append(UpdatePlanOutcome(
                outcome="rejected", name=name,
                reason="malformed_missing_program",
            ))
            lines.append(strings["update_plan_error_malformed"])
            continue
        if tok.unclosed_program and tok.open_span is not None:
            outcomes.append(UpdatePlanOutcome(
                outcome="rejected", name=name,
                reason="malformed_unclosed_program",
            ))
            lines.append(strings["update_plan_error_malformed"])
            continue

        program_raw = (tok.program_raw or "").strip()
        if len(program_raw.encode("utf-8", errors="replace")) > _MAX_PROGRAM_BYTES:
            outcomes.append(UpdatePlanOutcome(
                outcome="rejected", name=name, reason="bad_json",
                detail="program payload exceeds %d-byte cap" % _MAX_PROGRAM_BYTES,
            ))
            lines.append(strings["update_plan_error_bad_json"])
            log.warning(
                "update_plan bad_json reason=oversize name=%s bytes=%d",
                name, len(program_raw),
            )
            continue
        try:
            program = json.loads(program_raw) if program_raw else {}
        except (ValueError, TypeError) as exc:
            outcomes.append(UpdatePlanOutcome(
                outcome="rejected", name=name,
                reason="bad_json", detail=str(exc),
            ))
            lines.append(strings["update_plan_error_bad_json"])
            log.warning("update_plan bad_json name=%s detail=%s", name, exc)
            continue

        if not isinstance(program, dict):
            outcomes.append(UpdatePlanOutcome(
                outcome="rejected", name=name,
                reason="bad_json", detail="program is not an object",
            ))
            lines.append(strings["update_plan_error_bad_json"])
            continue

        overrides, bad_field = _validate_overrides(program)
        if bad_field is not None:
            outcomes.append(UpdatePlanOutcome(
                outcome="rejected", name=name,
                reason="bad_field", detail=bad_field,
            ))
            lines.append(strings["update_plan_error_bad_field"] % bad_field)
            log.warning("update_plan bad_field name=%s field=%s", name, bad_field)
            continue

        # Resolve and commit under the same per-name transaction used by
        # lifecycle operations and continuous creation. This prevents a
        # concurrent STOP/DELETE/UPDATE_PLAN read-modify-write collision.
        try:
            from lifecycle_macros import _lifecycle_task_lock

            async with _lifecycle_task_lock(name):
                state_path, state = _find_task(name, ctx)
                if state is None:
                    outcome = UpdatePlanOutcome(
                        outcome="rejected", name=name, reason="not_found",
                    )
                elif not overrides:
                    # Unknown/empty fields are a no-op, but lookup/scoping must
                    # still happen before reporting success.
                    _ensure_queue_scope(name, ctx)
                    if ctx.state_writer is None and ctx.task_scope is not None:
                        from pathlib import Path
                        from continuous import save_state

                        save_state(Path(state_path), state)
                    outcome = UpdatePlanOutcome(
                        outcome="applied", name=name,
                        detail="no overrides provided",
                    )
                else:
                    _ensure_queue_scope(name, ctx)
                    merged_program = dict(state.get("program") or {})
                    applied_overrides = dict(overrides)
                    plan_text_override = applied_overrides.pop("plan_text", None)
                    merged_program.update(applied_overrides)

                    # If the task was parked awaiting input, redirecting the
                    # program makes the old question obsolete.
                    if state.get("status") in ("awaiting-input", "awaiting_input"):
                        state["status"] = "pending"
                        state.pop("awaiting_question", None)
                        log.info(
                            "update_plan: cleared awaiting-input on '%s' "
                            "(plan redirected)",
                            name,
                        )

                    plan_body = (
                        plan_text_override
                        if plan_text_override is not None
                        else _render_plan_md(name, merged_program)
                    )

                    if ctx.state_writer is not None:
                        state["program"] = merged_program
                        state["updated_at"] = datetime.now(timezone.utc).isoformat()
                        ctx.state_writer(state_path, state)
                    else:
                        from pathlib import Path
                        from continuous import update_program

                        update_program(
                            Path(state_path),
                            state,
                            merged_program,
                            plan_text=plan_body,
                        )

                    if ctx.plan_writer is not None:
                        ctx.plan_writer(name, plan_body)
                    elif ctx.state_writer is not None:
                        from continuous import write_plan_md

                        write_plan_md(name, plan_body)
                    outcome = UpdatePlanOutcome(outcome="applied", name=name)
        except Exception as exc:
            outcome = UpdatePlanOutcome(
                outcome="rejected", name=name,
                reason="downstream_error", detail=str(exc),
            )
            log.error(
                "update_plan write failed name=%s exc=%s",
                name, exc, exc_info=True,
            )

        outcomes.append(outcome)
        if outcome.outcome == "applied":
            lines.append(strings["update_plan_ok"] % name)
            log.info(
                "update_plan applied name=%s fields=%s",
                name, sorted(overrides.keys()),
            )
        elif outcome.reason == "not_found":
            lines.append(strings["update_plan_error_not_found"] % name)
            log.info("update_plan not_found name=%s", name)
        else:
            lines.append(strings["update_plan_error_downstream"])

    parts = [p for p in (stripped,) if p]
    parts.extend(lines)
    out = "\n\n".join(parts)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out, outcomes


# ── User-facing scrub (belt + suspenders) ───────────────────────────────────


def strip_update_plan_macros(text: str) -> tuple[str, int]:
    """Defense-in-depth stripping for non-interactive paths.

    Returns ``(stripped, count)`` so callers can decide log severity.
    """
    stripped, tokens = extract_update_plan_macros(text)
    return stripped, len(tokens)


__all__ = [
    "REJECT_REASONS",
    "RejectReason",
    "UpdatePlanTokens",
    "UpdatePlanOutcome",
    "UpdatePlanContext",
    "extract_update_plan_macros",
    "apply_update_plan_macros",
    "strip_update_plan_macros",
]
