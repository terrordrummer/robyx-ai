"""Robyx — Continuous-task macro extraction and application.

Owns detection, stripping, validation, and side-effect dispatch for the
``[CREATE_CONTINUOUS ...] / [CONTINUOUS_PROGRAM] ... [/CONTINUOUS_PROGRAM]``
macro. Every terminal response path (orchestrator, workspace-agent,
collaborative-executive, scheduled delivery) runs its final assembled text
through ``apply_continuous_macros`` before the text reaches a platform adapter
or the TTS renderer.

Detection is tolerant (case-insensitive tags, straight or curly quotes, code
fences, realistic whitespace). Stripping is unconditional — any partial or
malformed match is scrubbed so the failure mode is "not executed", never
"leaked". Substitution is always one short prose line per detected macro
(confirmation on success, prose error on rejection), pulled from ``i18n``.

Contracts:
  - ``specs/004-fix-continuous-task-macro/contracts/continuous-macro-grammar.md``
  - ``specs/004-fix-continuous-task-macro/contracts/extract-continuous-macros.md``
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("robyx.continuous_macro")


# ── Reject reason identifiers ───────────────────────────────────────────────

# Stable machine tags used for logs AND for picking the i18n substitution key.
# Kept as a plain tuple (Python 3.10 compatibility; no StrEnum) and exposed
# as a ``Literal`` type alias for precise typing at call sites.
REJECT_REASONS: tuple[str, ...] = (
    "malformed_missing_open",
    "malformed_missing_program",
    "malformed_unclosed_program",
    "bad_json",
    "missing_field",
    "path_denied",
    "invalid_work_dir",
    "name_taken",
    "permission_denied",
    "downstream_error",
)

RejectReason = Literal[
    "malformed_missing_open",
    "malformed_missing_program",
    "malformed_unclosed_program",
    "bad_json",
    "missing_field",
    "path_denied",
    "invalid_work_dir",
    "name_taken",
    "permission_denied",
    "downstream_error",
]


# ── Data types ──────────────────────────────────────────────────────────────


@dataclass
class ContinuousMacroTokens:
    """One detected macro (may be opener-only, program-only, or both).

    See ``data-model.md`` for the field contract.

    ``unclosed_program`` is ``True`` when the program opener was detected
    but no matching ``[/CONTINUOUS_PROGRAM]`` was found; in that case
    ``program_span`` extends to end-of-text so the payload cannot leak.
    """

    open_span: tuple[int, int] | None = None
    program_span: tuple[int, int] | None = None
    name_raw: str | None = None
    work_dir_raw: str | None = None
    program_raw: str | None = None
    surrounding_fence: tuple[int, int] | None = None
    unclosed_program: bool = False


@dataclass
class ContinuousMacroOutcome:
    """Result of processing one detected macro."""

    outcome: Literal["intercepted", "rejected"]
    name: str = "?"
    thread_id: Any = None
    branch: str | None = None
    reason: RejectReason | None = None
    detail: str | None = None


@dataclass
class ApplyContext:
    """Parameters threaded into ``apply_continuous_macros``.

    Kept as a small dataclass so call sites don't balloon into ten-argument
    function calls. Every field is consumed by at least one side-effect
    branch in ``apply_continuous_macros``.
    """

    agent: Any
    thread_id: Any
    chat_id: Any
    platform: Any
    manager: Any
    is_executive: bool = True
    # Optional override for tests: a callable with the same signature as
    # ``topics.create_continuous_workspace``. If ``None``, the real one is
    # imported lazily to avoid circular-import pain at module load time.
    create_continuous_workspace: Any = None


# ── Tolerant regex (detection primitives) ───────────────────────────────────


# Attribute-value delimiter class: ASCII double-quote OR curly doubles OR
# curly singles. Mixed pairs are tolerated because some tokenizers open with
# one variant and close with another.
_QUOTE = r'["\u201C\u201D\u2018\u2019]'

# ``[CREATE_CONTINUOUS name="..." work_dir="..."]``
# Case-insensitive; ``\s+`` between tag token and attributes and between
# attributes (covers newlines); attribute order fixed as per grammar.
_CREATE_CONTINUOUS_RE = re.compile(
    r'\[\s*CREATE_CONTINUOUS'
    r'\s+name\s*=\s*' + _QUOTE + r'([^"\u201C\u201D\u2018\u2019]+)' + _QUOTE +
    r'\s+work_dir\s*=\s*' + _QUOTE + r'([^"\u201C\u201D\u2018\u2019]+)' + _QUOTE +
    r'\s*\]',
    re.IGNORECASE | re.DOTALL,
)

# ``[CONTINUOUS_PROGRAM] ... [/CONTINUOUS_PROGRAM]`` — paired form.
_CONTINUOUS_PROGRAM_PAIR_RE = re.compile(
    r'\[\s*CONTINUOUS_PROGRAM\s*\](.*?)\[\s*/\s*CONTINUOUS_PROGRAM\s*\]',
    re.IGNORECASE | re.DOTALL,
)

# Opening-only ``[CONTINUOUS_PROGRAM]`` (used to detect unclosed blocks).
_CONTINUOUS_PROGRAM_OPEN_RE = re.compile(
    r'\[\s*CONTINUOUS_PROGRAM\s*\]',
    re.IGNORECASE,
)
_CONTINUOUS_PROGRAM_CLOSE_RE = re.compile(
    r'\[\s*/\s*CONTINUOUS_PROGRAM\s*\]',
    re.IGNORECASE,
)

# Triple-backtick fence that wraps a macro. We only strip the fences when the
# fenced content is *exactly* whitespace + macro + whitespace, so we don't
# accidentally remove a fence that contained other content.
_FENCE_RE = re.compile(
    r'(?P<fence_open>```[a-zA-Z0-9_-]*\s*\n?)(?P<body>.*?)(?P<fence_close>\n?```)',
    re.DOTALL,
)


# ── Pure extraction ─────────────────────────────────────────────────────────


def extract_continuous_macros(
    text: str,
) -> tuple[str, list[ContinuousMacroTokens]]:
    """Detect and strip every continuous-task macro in ``text``.

    Returns ``(stripped, tokens)``:
      - ``stripped``: ``text`` with every detected macro span removed,
        surrounding code-fence wrappers removed when the fence contained
        only the macro, runs of ≥3 newlines collapsed to 2.
      - ``tokens``: zero or more ``ContinuousMacroTokens`` in source order.

    Pure function: no I/O, no logging, no network, no ``json.loads``.
    Idempotent: ``extract(extract(x)[0])[0] == extract(x)[0]``.
    """
    if not text:
        return text, []

    # Pass 1 — locate every opener, every paired program block, every
    # stray program opener (possibly unclosed), in source order.
    opens = list(_CREATE_CONTINUOUS_RE.finditer(text))
    pairs = list(_CONTINUOUS_PROGRAM_PAIR_RE.finditer(text))
    paired_open_starts = {m.start() for m in pairs}
    stray_opens: list[re.Match[str]] = []
    for m in _CONTINUOUS_PROGRAM_OPEN_RE.finditer(text):
        # Skip openers that belong to a successful pair match.
        if any(
            p.start() <= m.start() < p.start() + len(
                m.group(0)  # length of "[CONTINUOUS_PROGRAM]"
            ) + 1
            for p in pairs
        ):
            continue
        if m.start() in paired_open_starts:
            continue
        # Confirm it's not nested inside a completed pair (by end offset).
        inside_pair = any(p.start() <= m.start() < p.end() for p in pairs)
        if inside_pair:
            continue
        stray_opens.append(m)

    # Pass 2 — pair up opener tokens with program blocks by source order.
    # Strategy: iterate source positions, greedily match each opener with
    # the next program block whose start is after the opener's end.
    tokens: list[ContinuousMacroTokens] = []

    used_pair_idx: set[int] = set()
    used_stray_idx: set[int] = set()

    for open_m in opens:
        tok = ContinuousMacroTokens(
            open_span=(open_m.start(), open_m.end()),
            name_raw=open_m.group(1),
            work_dir_raw=open_m.group(2),
        )
        # Prefer the closest following paired program block.
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
            # No paired block — is there a stray unclosed opener after us?
            for idx, s in enumerate(stray_opens):
                if idx in used_stray_idx:
                    continue
                if s.start() >= open_m.end():
                    used_stray_idx.add(idx)
                    # Unclosed: span extends to end-of-text so no JSON leaks.
                    tok.program_span = (s.start(), len(text))
                    tok.program_raw = text[s.end():]
                    tok.unclosed_program = True
                    break
        tokens.append(tok)

    # Pairs that never matched any opener — record as "program without open".
    for idx, p in enumerate(pairs):
        if idx in used_pair_idx:
            continue
        tokens.append(
            ContinuousMacroTokens(
                program_span=(p.start(), p.end()),
                program_raw=p.group(1),
            )
        )

    # Strays that never matched any opener — record as unclosed program
    # without open. Span extends to end-of-text to prevent JSON leakage.
    for idx, s in enumerate(stray_opens):
        if idx in used_stray_idx:
            continue
        tokens.append(
            ContinuousMacroTokens(
                program_span=(s.start(), len(text)),
                program_raw=text[s.end():],
                unclosed_program=True,
            )
        )

    if not tokens:
        return text, []

    # Pass 3 — detect surrounding code fences for macros whose fenced body
    # contains nothing but the macro (plus whitespace). Record the fence
    # span so stripping removes it too.
    for tok in tokens:
        span = _primary_span(tok)
        if span is None:
            continue
        fence_match = _enclosing_fence(text, span)
        if fence_match is not None:
            tok.surrounding_fence = fence_match

    # Sort tokens by their earliest-populated start offset.
    tokens.sort(key=lambda t: _primary_span(t)[0] if _primary_span(t) else 0)

    # Pass 4 — build the stripped text by removing every recorded span in
    # reverse order so earlier offsets remain valid.
    spans_to_remove: list[tuple[int, int]] = []
    for tok in tokens:
        if tok.surrounding_fence is not None:
            spans_to_remove.append(tok.surrounding_fence)
            continue
        if tok.open_span is not None:
            spans_to_remove.append(tok.open_span)
        if tok.program_span is not None:
            spans_to_remove.append(tok.program_span)
    # Merge and reverse-sort for safe deletion.
    spans_to_remove = _merge_spans(spans_to_remove)
    spans_to_remove.sort(reverse=True)
    stripped = text
    for start, end in spans_to_remove:
        stripped = stripped[:start] + stripped[end:]

    # Collapse runs of ≥3 newlines (matches the existing normalization rule
    # applied throughout ``handlers.py`` / ``scheduled_delivery.py``).
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()

    return stripped, tokens


def _primary_span(tok: ContinuousMacroTokens) -> tuple[int, int] | None:
    if tok.open_span is not None and tok.program_span is not None:
        return (
            min(tok.open_span[0], tok.program_span[0]),
            max(tok.open_span[1], tok.program_span[1]),
        )
    return tok.open_span or tok.program_span


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping / adjacent spans so deletion never double-counts."""
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
    """Return the span of a triple-backtick fence that wraps ``inner_span``
    with nothing else besides whitespace. Returns ``None`` if no such
    fence exists.
    """
    for m in _FENCE_RE.finditer(text):
        body_start = m.start("body")
        body_end = m.end("body")
        if body_start > inner_span[0] or body_end < inner_span[1]:
            continue
        # Content outside the inner macro must be whitespace only.
        before = text[body_start:inner_span[0]]
        after = text[inner_span[1]:body_end]
        if before.strip() or after.strip():
            continue
        return (m.start(), m.end())
    return None


# ── Side-effect application ─────────────────────────────────────────────────


# Required program-payload keys. Absence → Rejected(missing_field).
_REQUIRED_PROGRAM_FIELDS = ("objective", "success_criteria", "first_step")


def _lazy_create_continuous_workspace():
    """Import the real side-effect entry point at call time to avoid
    circular-import issues (``topics`` imports from ``continuous`` which
    imports from ``config``; ``handlers`` imports from here)."""
    from topics import create_continuous_workspace
    return create_continuous_workspace


def _lazy_strings() -> dict:
    from i18n import STRINGS
    return STRINGS


def _lazy_workspace_root() -> Path:
    from config import WORKSPACE
    return Path(WORKSPACE).resolve()


async def apply_continuous_macros(
    response: str,
    ctx: ApplyContext,
) -> tuple[str, list[ContinuousMacroOutcome]]:
    """Extract, validate, and dispatch every continuous-task macro in
    ``response``. Returns ``(user_visible_response, outcomes)``.

    Contract: the returned ``user_visible_response`` MUST NOT contain any
    macro tokens or JSON payload from the original ``response``. For every
    detected macro, one prose line from ``i18n.STRINGS`` is appended — a
    confirmation on success, a short error on rejection. No exception
    escapes: downstream failures become ``Rejected(downstream_error)``.
    """
    stripped, tokens = extract_continuous_macros(response)
    if not tokens:
        return response, []

    outcomes: list[ContinuousMacroOutcome] = []
    strings = _lazy_strings()
    workspace_root = _lazy_workspace_root()
    create_ws = ctx.create_continuous_workspace or _lazy_create_continuous_workspace()

    lines: list[str] = []

    for tok in tokens:
        # ── 1. Permission gate (defense in depth) ──────────────────────
        if not ctx.is_executive:
            name = tok.name_raw or "?"
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=name,
                reason="permission_denied",
            )
            outcomes.append(outcome)
            lines.append(strings["continuous_task_error_permission_denied"])
            _log_outcome(ctx, outcome)
            continue

        # ── 2. Structural gate ─────────────────────────────────────────
        if tok.open_span is None and tok.program_span is not None:
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name="?",
                reason="malformed_missing_open",
            )
            outcomes.append(outcome)
            lines.append(strings["continuous_task_error_malformed"])
            _log_outcome(ctx, outcome)
            continue

        if tok.open_span is not None and tok.program_span is None:
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=tok.name_raw or "?",
                reason="malformed_missing_program",
            )
            outcomes.append(outcome)
            lines.append(strings["continuous_task_error_malformed"])
            _log_outcome(ctx, outcome)
            continue

        # Unclosed detection: the extraction layer set this flag when an
        # opening ``[CONTINUOUS_PROGRAM]`` was detected without a matching
        # ``[/CONTINUOUS_PROGRAM]``. Well-formed paired blocks leave it False.
        if tok.unclosed_program and tok.open_span is not None:
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=tok.name_raw or "?",
                reason="malformed_unclosed_program",
            )
            outcomes.append(outcome)
            lines.append(strings["continuous_task_error_malformed"])
            _log_outcome(ctx, outcome)
            continue

        # ── 3. JSON parse ──────────────────────────────────────────────
        try:
            program = json.loads((tok.program_raw or "").strip())
        except (ValueError, TypeError) as exc:
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=tok.name_raw or "?",
                reason="bad_json",
                detail=str(exc),
            )
            outcomes.append(outcome)
            lines.append(strings["continuous_task_error_bad_json"])
            _log_outcome(ctx, outcome)
            log.warning("continuous.macro bad_json detail=%s", exc)
            continue

        if not isinstance(program, dict):
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=tok.name_raw or "?",
                reason="bad_json",
                detail="program is not an object",
            )
            outcomes.append(outcome)
            lines.append(strings["continuous_task_error_bad_json"])
            _log_outcome(ctx, outcome)
            continue

        # ── 4. Required-field validation ───────────────────────────────
        missing = _first_missing_field(program)
        if missing is not None:
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=tok.name_raw or "?",
                reason="missing_field",
                detail=missing,
            )
            outcomes.append(outcome)
            lines.append(
                strings["continuous_task_error_missing_field"] % missing
            )
            _log_outcome(ctx, outcome)
            continue

        # ── 5. work_dir resolution + confinement ──────────────────────
        work_dir = tok.work_dir_raw or ""
        try:
            resolved_wd = Path(work_dir).resolve()
        except (OSError, ValueError):
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=tok.name_raw or "?",
                reason="invalid_work_dir",
            )
            outcomes.append(outcome)
            lines.append(strings["continuous_task_error_path_denied"])
            _log_outcome(ctx, outcome)
            continue
        try:
            resolved_wd.relative_to(workspace_root)
        except ValueError:
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=tok.name_raw or "?",
                reason="path_denied",
            )
            outcomes.append(outcome)
            lines.append(strings["continuous_task_error_path_denied"])
            _log_outcome(ctx, outcome)
            continue

        # ── 6. Parent-workspace resolution ────────────────────────────
        parent_ws_name = _resolve_parent_workspace(ctx)

        # ── 7. Side effects ──────────────────────────────────────────
        name = tok.name_raw or "?"
        try:
            result = await create_ws(
                name=name,
                program=program,
                work_dir=work_dir,
                parent_workspace=parent_ws_name,
                model="powerful",
                manager=ctx.manager,
                platform=ctx.platform,
            )
        except ValueError as exc:
            msg = str(exc)
            if msg.lower().startswith("name taken") or "already" in msg.lower():
                reason: RejectReason = "name_taken"
            else:
                reason = "downstream_error"
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=name,
                reason=reason,
                detail=msg,
            )
            outcomes.append(outcome)
            if reason == "name_taken":
                lines.append(
                    strings["continuous_task_error_name_taken"] % name
                )
            else:
                lines.append(strings["continuous_task_error_downstream"])
            _log_outcome(ctx, outcome)
            log.warning("continuous.macro downstream name=%s detail=%s", name, msg)
            continue
        except Exception as exc:
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=name,
                reason="downstream_error",
                detail=str(exc),
            )
            outcomes.append(outcome)
            lines.append(strings["continuous_task_error_downstream"])
            _log_outcome(ctx, outcome)
            log.error(
                "continuous.macro downstream name=%s exc=%s",
                name, exc, exc_info=True,
            )
            continue

        if not result:
            outcome = ContinuousMacroOutcome(
                outcome="rejected",
                name=name,
                reason="downstream_error",
                detail="create_continuous_workspace returned falsy",
            )
            outcomes.append(outcome)
            lines.append(strings["continuous_task_error_downstream"])
            _log_outcome(ctx, outcome)
            continue

        display = result.get("display_name", name)
        thread_id = result.get("thread_id")
        branch = result.get("branch", "")
        outcome = ContinuousMacroOutcome(
            outcome="intercepted",
            name=display,
            thread_id=thread_id,
            branch=branch,
        )
        outcomes.append(outcome)
        lines.append(
            strings["continuous_task_created"] % (display, thread_id, branch)
        )
        _log_outcome(ctx, outcome)

    # Assemble user-visible response: stripped text + one prose line per outcome.
    parts = [p for p in (stripped,) if p]
    parts.extend(lines)
    out = "\n\n".join(parts)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out, outcomes


def _first_missing_field(program: dict) -> str | None:
    for key in _REQUIRED_PROGRAM_FIELDS:
        if key not in program:
            return key
    objective = program.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        return "objective"
    criteria = program.get("success_criteria")
    if not isinstance(criteria, list) or len(criteria) < 1:
        return "success_criteria"
    first_step = program.get("first_step")
    if not isinstance(first_step, dict):
        return "first_step"
    desc = first_step.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return "first_step"
    return None


def _resolve_parent_workspace(ctx: ApplyContext) -> str:
    """Resolve ``parent_workspace`` with the existing fallback to ``"robyx"``.

    The existing behavior (handlers.py:1049–1058) is preserved: look up the
    emitting thread in the agent manager; if no workspace is mapped, fall
    back to ``"robyx"``. The fallback is intentional and MUST stay.
    """
    manager = ctx.manager
    thread_id = ctx.thread_id
    if manager is None or thread_id is None:
        return "robyx"
    try:
        parent = manager.get_by_thread(thread_id)
    except Exception:
        parent = None
    if parent is not None and getattr(parent, "name", None):
        return parent.name
    return "robyx"


def _log_outcome(ctx: ApplyContext, outcome: ContinuousMacroOutcome) -> None:
    agent_name = getattr(ctx.agent, "name", "?")
    if outcome.outcome == "intercepted":
        log.info(
            "continuous.macro outcome=intercepted agent=%s name=%s "
            "thread_id=%s branch=%s",
            agent_name, outcome.name, outcome.thread_id, outcome.branch,
        )
    else:
        log.info(
            "continuous.macro outcome=rejected agent=%s name=%s reason=%s",
            agent_name, outcome.name, outcome.reason,
        )


# ── Scheduled-delivery helper ───────────────────────────────────────────────


def strip_continuous_macros_for_log(text: str) -> tuple[str, int]:
    """Defense-in-depth stripping for non-interactive paths.

    Scheduled subprocess output may contain a leaked macro. Those paths have
    no interactive agent context (no ``ApplyContext``), so they MUST NOT
    dispatch a new continuous task — but they MUST still scrub the tokens
    so the macro does not reach the chat. Returns ``(stripped_text, count)``
    where ``count`` is the number of tokens detected (for log-level
    decisions).
    """
    stripped, tokens = extract_continuous_macros(text)
    if tokens:
        log.warning(
            "continuous.macro stray-tokens path=scheduled count=%d",
            len(tokens),
        )
    return stripped, len(tokens)


__all__ = [
    "REJECT_REASONS",
    "RejectReason",
    "ContinuousMacroTokens",
    "ContinuousMacroOutcome",
    "ApplyContext",
    "extract_continuous_macros",
    "apply_continuous_macros",
    "strip_continuous_macros_for_log",
]
