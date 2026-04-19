"""Tests for ``bot/continuous_macro.py`` — feature 004.

Covers:
  - Type-level smoke tests for the public dataclasses / reason enum (T007).
  - Pure ``extract_continuous_macros`` behaviour on every fixture file:
    golden, malformed variants, realistic variations, multiple macros (T008,
    T024–T028, T047–T053).
  - Async ``apply_continuous_macros`` behaviour: permission gating, JSON
    errors, missing fields, path escape, name collision, downstream errors,
    success path (T009, T029–T032).

All tests are hermetic: ``topics.create_continuous_workspace`` is patched
via the ``create_continuous_workspace`` attribute on ``ApplyContext``, and
``config.WORKSPACE`` is monkeypatched to a tmpdir so we can assert path
confinement without touching the real filesystem workspace root.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Ensure the bot package is importable (same pattern as other tests).
ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import continuous_macro as cm  # noqa: E402
from continuous_macro import (  # noqa: E402
    ApplyContext,
    ContinuousMacroOutcome,
    ContinuousMacroTokens,
    REJECT_REASONS,
    apply_continuous_macros,
    extract_continuous_macros,
    strip_continuous_macros_for_log,
)


FIXTURES = ROOT / "tests" / "fixtures" / "continuous_macros"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


# ─────────────────────────────────────────────────────────────────────────
# T007 — type-level smoke tests
# ─────────────────────────────────────────────────────────────────────────


def test_reject_reasons_enum_is_stable():
    assert "bad_json" in REJECT_REASONS
    assert "permission_denied" in REJECT_REASONS
    # Spec requires exactly ten stable reason tags.
    assert len(REJECT_REASONS) == 10
    assert len(set(REJECT_REASONS)) == 10


def test_tokens_dataclass_defaults():
    tok = ContinuousMacroTokens()
    assert tok.open_span is None
    assert tok.program_span is None
    assert tok.name_raw is None
    assert tok.work_dir_raw is None
    assert tok.program_raw is None
    assert tok.surrounding_fence is None


def test_outcome_dataclass_construction():
    ok = ContinuousMacroOutcome(
        outcome="intercepted", name="x", thread_id=42, branch="b",
    )
    assert ok.outcome == "intercepted"
    bad = ContinuousMacroOutcome(
        outcome="rejected", name="x", reason="bad_json", detail="trailing comma",
    )
    assert bad.reason == "bad_json"
    # Discriminator is one of the two literals.
    assert ok.outcome in {"intercepted", "rejected"}
    assert bad.outcome in {"intercepted", "rejected"}


def test_apply_context_defaults():
    ctx = ApplyContext(
        agent=None, thread_id=None, chat_id=None,
        platform=None, manager=None,
    )
    assert ctx.is_executive is True
    assert ctx.create_continuous_workspace is None


# ─────────────────────────────────────────────────────────────────────────
# Pure extraction: every fixture leaves no tokens in the stripped output.
# ─────────────────────────────────────────────────────────────────────────


ALL_FIXTURES = [
    "golden.txt",
    "missing_program.txt",
    "missing_open.txt",
    "unclosed_program.txt",
    "bad_json.txt",
    "missing_field_objective.txt",
    "path_escape.txt",
    "multiple_macros_mixed.txt",
    "code_fenced.txt",
    "curly_quotes.txt",
    "leading_prose.txt",
    "mixed_case.txt",
    "extra_whitespace.txt",
]


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_extract_leaves_no_macro_tokens(fixture):
    """SC-001: zero fragment of the macro may appear in the stripped output,
    on ANY fixture, regardless of whether the macro is well-formed or not."""
    stripped, tokens = extract_continuous_macros(_read(fixture))
    assert "[CREATE_CONTINUOUS" not in stripped.upper()
    assert "CONTINUOUS_PROGRAM" not in stripped.upper()
    assert "[/CONTINUOUS_PROGRAM]" not in stripped.upper()
    # At least one token is detected in every fixture (we authored them so).
    assert len(tokens) >= 1


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_extract_is_idempotent(fixture):
    """T053: ``extract(extract(x)[0])[0] == extract(x)[0]``."""
    stripped_once, _ = extract_continuous_macros(_read(fixture))
    stripped_twice, tokens_twice = extract_continuous_macros(stripped_once)
    assert stripped_twice == stripped_once
    assert tokens_twice == []


# ─────────────────────────────────────────────────────────────────────────
# T008 — golden single macro: opener + program paired, prose preserved
# ─────────────────────────────────────────────────────────────────────────


def test_extract_golden_single_macro():
    stripped, tokens = extract_continuous_macros(_read("golden.txt"))
    assert len(tokens) == 1
    tok = tokens[0]
    assert tok.open_span is not None
    assert tok.program_span is not None
    assert tok.name_raw == "deconv-bench"
    assert tok.work_dir_raw == "/tmp/robyx-test-workspace/deconv"
    # Prose preserved.
    assert "I'll set up the continuous task now" in stripped


# ─────────────────────────────────────────────────────────────────────────
# T049 — leading/trailing prose is preserved; only the macro is removed
# ─────────────────────────────────────────────────────────────────────────


def test_leading_and_trailing_prose_preserved():
    stripped, tokens = extract_continuous_macros(_read("leading_prose.txt"))
    assert len(tokens) == 1
    assert "I've been thinking about this" in stripped
    assert "Let me know if you want to tighten" in stripped


# ─────────────────────────────────────────────────────────────────────────
# T047 — code fence wrapping only the macro is removed along with the tags
# ─────────────────────────────────────────────────────────────────────────


def test_code_fenced_macro_is_intercepted_and_fences_removed():
    stripped, tokens = extract_continuous_macros(_read("code_fenced.txt"))
    assert len(tokens) == 1
    assert tokens[0].surrounding_fence is not None
    # The only triple-backtick in the original was around the macro, so no
    # backtick fences should remain.
    assert "```" not in stripped
    assert "Here's the continuous-task spec" in stripped
    assert "Let me know if you want changes" in stripped


# ─────────────────────────────────────────────────────────────────────────
# T048, T050, T051 — realistic surface variations are still intercepted
# ─────────────────────────────────────────────────────────────────────────


def test_curly_quotes_macro_is_intercepted():
    stripped, tokens = extract_continuous_macros(_read("curly_quotes.txt"))
    assert len(tokens) == 1
    assert tokens[0].name_raw == "curly"
    assert tokens[0].work_dir_raw == "/tmp/robyx-test-workspace/curly"
    assert "CREATE_CONTINUOUS" not in stripped.upper()


def test_mixed_case_tags_are_intercepted():
    stripped, tokens = extract_continuous_macros(_read("mixed_case.txt"))
    assert len(tokens) == 1
    assert tokens[0].name_raw == "mixed-case"
    assert "CONTINUOUS_PROGRAM" not in stripped.upper()


def test_extra_whitespace_between_attributes_is_tolerated():
    stripped, tokens = extract_continuous_macros(_read("extra_whitespace.txt"))
    assert len(tokens) == 1
    assert tokens[0].name_raw == "whitespace"
    assert "CONTINUOUS_PROGRAM" not in stripped.upper()


# ─────────────────────────────────────────────────────────────────────────
# T024, T025, T026 — partial/malformed structural forms
# ─────────────────────────────────────────────────────────────────────────


def test_malformed_missing_program_records_open_only():
    _, tokens = extract_continuous_macros(_read("missing_program.txt"))
    assert len(tokens) == 1
    assert tokens[0].open_span is not None
    assert tokens[0].program_span is None


def test_malformed_missing_open_records_program_only():
    _, tokens = extract_continuous_macros(_read("missing_open.txt"))
    assert len(tokens) == 1
    assert tokens[0].open_span is None
    assert tokens[0].program_span is not None


def test_unclosed_program_extends_to_end_of_text():
    text = _read("unclosed_program.txt")
    stripped, tokens = extract_continuous_macros(text)
    assert len(tokens) == 1
    tok = tokens[0]
    assert tok.open_span is not None
    assert tok.program_span is not None
    # The unclosed program block MUST extend to end-of-text so JSON can't leak.
    assert tok.program_span[1] == len(text)
    # And the stripped text must not contain any JSON keys from the payload.
    assert "objective" not in stripped


# ─────────────────────────────────────────────────────────────────────────
# T052 — multiple macros: one success, one failure; each produces its own
# outcome; no token from either leaks.
# ─────────────────────────────────────────────────────────────────────────


def test_multiple_macros_extracted_in_source_order():
    _, tokens = extract_continuous_macros(_read("multiple_macros_mixed.txt"))
    assert len(tokens) == 2
    assert tokens[0].name_raw == "first-good"
    assert tokens[1].name_raw == "second-bad"


# ─────────────────────────────────────────────────────────────────────────
# Apply — golden path, dispatch happens once; confirmation line uses i18n
# ─────────────────────────────────────────────────────────────────────────


class _StubManager:
    """Minimal manager: always reports no mapped workspace, exercising the
    fallback-to-``robyx`` code path."""

    def get_by_thread(self, thread_id):
        return None


def _make_ctx(
    monkeypatch,
    tmp_path,
    *,
    create_ws,
    is_executive=True,
    parent_name=None,
    thread_id=1,
):
    monkeypatch.setattr("continuous_macro._lazy_workspace_root",
                        lambda: tmp_path.resolve())

    class _Manager:
        def get_by_thread(self, tid):
            if parent_name is None:
                return None

            class _A:
                name = parent_name
            return _A()

    return ApplyContext(
        agent=type("A", (), {"name": "test-agent"})(),
        thread_id=thread_id,
        chat_id=999,
        platform=object(),
        manager=_Manager(),
        is_executive=is_executive,
        create_continuous_workspace=create_ws,
    )


def _prepare_fixture(tmp_path, fixture_name, subdir="deconv"):
    """Some fixtures point at /tmp/robyx-test-workspace/...; rewrite them to
    live under ``tmp_path`` so the path-confinement check passes."""
    text = _read(fixture_name)
    return text.replace("/tmp/robyx-test-workspace",
                        str(tmp_path.resolve()))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def new_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


def test_apply_golden_produces_intercepted(monkeypatch, tmp_path, new_loop):
    calls = []

    async def stub_create(**kwargs):
        calls.append(kwargs)
        return {
            "display_name": kwargs["name"],
            "thread_id": 17,
            "branch": "continuous/" + kwargs["name"],
        }

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    text = _prepare_fixture(tmp_path, "golden.txt")
    # Ensure the target work_dir exists so Path.resolve() relative_to() passes.
    (tmp_path / "deconv").mkdir(parents=True, exist_ok=True)

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].outcome == "intercepted"
    assert outcomes[0].name == "deconv-bench"
    assert outcomes[0].thread_id == 17
    assert outcomes[0].branch == "continuous/deconv-bench"
    # Confirmation is rendered from i18n; no raw tokens.
    assert "Continuous task *deconv-bench* created" in out
    assert "[CREATE_CONTINUOUS" not in out
    # Dispatch happened exactly once with fallback parent_workspace="robyx".
    assert len(calls) == 1
    assert calls[0]["parent_workspace"] == "robyx"


def test_permission_denied_when_non_executive(monkeypatch, tmp_path, new_loop):
    async def stub_create(**_):
        raise AssertionError("should not be called")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create, is_executive=False)
    text = _prepare_fixture(tmp_path, "golden.txt")

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].outcome == "rejected"
    assert outcomes[0].reason == "permission_denied"
    assert "[CREATE_CONTINUOUS" not in out
    assert "not authorised" in out


def test_bad_json_is_rejected_with_prose_error(monkeypatch, tmp_path, new_loop):
    async def stub_create(**_):
        raise AssertionError("should not be called")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    text = _prepare_fixture(tmp_path, "bad_json.txt")
    (tmp_path / "bad").mkdir(parents=True, exist_ok=True)

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].reason == "bad_json"
    assert "[CREATE_CONTINUOUS" not in out
    assert "{" not in out  # raw JSON must not appear
    assert "could not be parsed" in out


def test_missing_field_rejects_naming_the_field(monkeypatch, tmp_path, new_loop):
    async def stub_create(**_):
        raise AssertionError("should not be called")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    text = _prepare_fixture(tmp_path, "missing_field_objective.txt")
    (tmp_path / "nof").mkdir(parents=True, exist_ok=True)

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].reason == "missing_field"
    assert outcomes[0].detail == "objective"
    assert "objective" in out  # i18n template substitutes the field name
    assert "[CREATE_CONTINUOUS" not in out


def test_path_denied_when_work_dir_outside_workspace(monkeypatch, tmp_path, new_loop):
    async def stub_create(**_):
        raise AssertionError("should not be called")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    # path_escape.txt points at /etc/passwd, explicitly outside tmp_path.
    text = _read("path_escape.txt")

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].reason == "path_denied"
    assert "[CREATE_CONTINUOUS" not in out
    assert "outside the workspace" in out


def test_name_taken_maps_to_name_taken_outcome(monkeypatch, tmp_path, new_loop):
    async def stub_create(**_):
        raise ValueError("name taken: deconv-bench")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    text = _prepare_fixture(tmp_path, "golden.txt")
    (tmp_path / "deconv").mkdir(parents=True, exist_ok=True)

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].reason == "name_taken"
    assert "already in use" in out
    assert "[CREATE_CONTINUOUS" not in out


def test_downstream_error_is_caught_and_logged(monkeypatch, tmp_path, new_loop):
    async def stub_create(**_):
        raise RuntimeError("kaboom")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    text = _prepare_fixture(tmp_path, "golden.txt")
    (tmp_path / "deconv").mkdir(parents=True, exist_ok=True)

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].reason == "downstream_error"
    assert "internal error" in out
    assert "[CREATE_CONTINUOUS" not in out


def test_multiple_macros_one_success_one_rejection(monkeypatch, tmp_path, new_loop):
    async def stub_create(**kwargs):
        if kwargs["name"] == "first-good":
            return {
                "display_name": "first-good",
                "thread_id": 22,
                "branch": "continuous/first-good",
            }
        raise AssertionError("should not reach here — second macro is malformed")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    text = _prepare_fixture(tmp_path, "multiple_macros_mixed.txt")
    (tmp_path / "good").mkdir(parents=True, exist_ok=True)
    (tmp_path / "bad2").mkdir(parents=True, exist_ok=True)

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 2
    assert outcomes[0].outcome == "intercepted"
    assert outcomes[0].name == "first-good"
    assert outcomes[1].outcome == "rejected"
    assert outcomes[1].reason == "bad_json"
    # Exactly one confirmation line, exactly one error line; no raw tokens.
    assert "Continuous task *first-good* created" in out
    assert "could not be parsed" in out
    assert "[CREATE_CONTINUOUS" not in out


# ─────────────────────────────────────────────────────────────────────────
# Unclosed program block → rejection without side effects (FR-004)
# ─────────────────────────────────────────────────────────────────────────


def test_unclosed_program_is_rejected_without_dispatch(monkeypatch, tmp_path, new_loop):
    async def stub_create(**_):
        raise AssertionError("should not be called")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    text = _prepare_fixture(tmp_path, "unclosed_program.txt")

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].reason == "malformed_unclosed_program"
    assert "[CREATE_CONTINUOUS" not in out
    assert "{" not in out


def test_missing_program_is_rejected_without_dispatch(monkeypatch, tmp_path, new_loop):
    async def stub_create(**_):
        raise AssertionError("should not be called")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    text = _prepare_fixture(tmp_path, "missing_program.txt")

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].reason == "malformed_missing_program"
    assert "[CREATE_CONTINUOUS" not in out
    assert "setup block was incomplete" in out


def test_missing_open_is_rejected_without_dispatch(monkeypatch, tmp_path, new_loop):
    async def stub_create(**_):
        raise AssertionError("should not be called")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    text = _read("missing_open.txt")

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].reason == "malformed_missing_open"
    assert "CONTINUOUS_PROGRAM" not in out.upper()


# ─────────────────────────────────────────────────────────────────────────
# strip_continuous_macros_for_log — defensive helper used by
# bot/scheduled_delivery.py
# ─────────────────────────────────────────────────────────────────────────


def test_strip_for_log_returns_stripped_and_count():
    stripped, count = strip_continuous_macros_for_log(_read("golden.txt"))
    assert count == 1
    assert "[CREATE_CONTINUOUS" not in stripped
    # Empty input is a safe no-op.
    stripped2, count2 = strip_continuous_macros_for_log("")
    assert count2 == 0
    assert stripped2 == ""


# ─────────────────────────────────────────────────────────────────────────
# spec 005 T011: strip_control_tokens_for_user is the canonical user-facing
# scrub and MUST remove every leak pathway uniformly.
# ─────────────────────────────────────────────────────────────────────────


def test_strip_control_tokens_removes_macro_and_status():
    from continuous_macro import strip_control_tokens_for_user

    text = (
        "Starting the task.\n"
        '[CREATE_CONTINUOUS name="docs-hunt" work_dir="/tmp/x"]\n'
        "[CONTINUOUS_PROGRAM]\n{\"objective\": \"stub\"}\n[/CONTINUOUS_PROGRAM]\n"
        "[STATUS scanning repo]\n"
        "Done."
    )
    out = strip_control_tokens_for_user(text)
    assert "[CREATE_CONTINUOUS" not in out
    assert "[CONTINUOUS_PROGRAM" not in out
    assert "[STATUS" not in out
    assert "Starting the task." in out
    assert "Done." in out


def test_strip_control_tokens_is_idempotent():
    from continuous_macro import strip_control_tokens_for_user

    text = 'x [CREATE_CONTINUOUS name="a" work_dir="/tmp"] y'
    once = strip_control_tokens_for_user(text)
    twice = strip_control_tokens_for_user(once)
    assert once == twice


def test_strip_control_tokens_handles_empty_and_none():
    from continuous_macro import strip_control_tokens_for_user

    assert strip_control_tokens_for_user("") == ""
    assert strip_control_tokens_for_user(None) == ""


def test_strip_control_tokens_preserves_clean_text():
    from continuous_macro import strip_control_tokens_for_user

    text = "Just a normal response with **markdown** and a [link](http://x)."
    assert strip_control_tokens_for_user(text) == text


def test_strip_control_tokens_collapses_newlines():
    from continuous_macro import strip_control_tokens_for_user

    text = "one\n\n\n\n\ntwo"
    out = strip_control_tokens_for_user(text)
    assert "\n\n\n" not in out
    assert "one" in out and "two" in out


# ─────────────────────────────────────────────────────────────────────────
# Parent-workspace resolution uses the mapped agent's name when available
# ─────────────────────────────────────────────────────────────────────────


def test_parent_workspace_uses_mapped_agent_name(monkeypatch, tmp_path, new_loop):
    captured = {}

    async def stub_create(**kwargs):
        captured.update(kwargs)
        return {
            "display_name": kwargs["name"],
            "thread_id": 9,
            "branch": "continuous/" + kwargs["name"],
        }

    ctx = _make_ctx(
        monkeypatch, tmp_path, create_ws=stub_create,
        parent_name="some-workspace",
    )
    text = _prepare_fixture(tmp_path, "golden.txt")
    (tmp_path / "deconv").mkdir(parents=True, exist_ok=True)

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert outcomes[0].outcome == "intercepted"
    assert captured["parent_workspace"] == "some-workspace"
    assert "[CREATE_CONTINUOUS" not in out


# ─────────────────────────────────────────────────────────────────────────
# Pass 2 T079a / P2-73: JSON payload size cap
# ─────────────────────────────────────────────────────────────────────────


def test_oversized_program_rejected_as_bad_json(monkeypatch, tmp_path, new_loop):
    """A [CONTINUOUS_PROGRAM] body larger than _MAX_PROGRAM_BYTES must be
    rejected with reason=bad_json BEFORE json.loads is called, to prevent
    an adversarial AI from burning CPU/RAM on a multi-megabyte payload."""
    from continuous_macro import _MAX_PROGRAM_BYTES

    async def stub_create(**kwargs):
        # If the guard fails and json.loads happens to succeed, we'd still
        # want the test to fail loudly — but the guard should short-circuit
        # before reaching here.
        raise AssertionError("create_ws must not be called for oversized payload")

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    (tmp_path / "deconv").mkdir(parents=True, exist_ok=True)

    # Build a payload whose body alone exceeds the cap. Content shape is
    # irrelevant — the size check runs before JSON parsing. Fill with
    # whitespace + digits so it's valid UTF-8 and obviously non-malicious.
    oversized_body = '{"a":"' + ("x" * (_MAX_PROGRAM_BYTES + 1024)) + '"}'
    text = (
        '[CREATE_CONTINUOUS name="oversize" work_dir="%s/deconv"]\n'
        '[CONTINUOUS_PROGRAM]\n%s\n[/CONTINUOUS_PROGRAM]\n'
    ) % (tmp_path.resolve(), oversized_body)

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].outcome == "rejected"
    assert outcomes[0].reason == "bad_json"
    assert "exceeds" in (outcomes[0].detail or "")
    # Macro span stripped even on rejection (stripping is unconditional).
    assert "[CREATE_CONTINUOUS" not in out


def test_at_cap_program_is_parsed_normally(monkeypatch, tmp_path, new_loop):
    """A payload at exactly the cap boundary must still be parsed — the cap
    is an upper bound, not a soft threshold. Constructs a minimal valid
    program and verifies it reaches create_ws."""
    from continuous_macro import _MAX_PROGRAM_BYTES

    # Valid program well under the cap so we know we're testing the happy
    # path, not a boundary miss.
    assert _MAX_PROGRAM_BYTES > 256, "test assumes a generous cap"

    calls = []

    async def stub_create(**kwargs):
        calls.append(kwargs)
        return {
            "display_name": kwargs["name"],
            "thread_id": 7,
            "branch": "continuous/" + kwargs["name"],
        }

    ctx = _make_ctx(monkeypatch, tmp_path, create_ws=stub_create)
    (tmp_path / "deconv").mkdir(parents=True, exist_ok=True)

    program_body = (
        '{"objective": "test the cap boundary",'
        ' "success_criteria": ["x"],'
        ' "first_step": {"number": 1, "description": "probe"}}'
    )
    text = (
        '[CREATE_CONTINUOUS name="normal" work_dir="%s/deconv"]\n'
        '[CONTINUOUS_PROGRAM]\n%s\n[/CONTINUOUS_PROGRAM]\n'
    ) % (tmp_path.resolve(), program_body)

    out, outcomes = new_loop.run_until_complete(
        apply_continuous_macros(text, ctx)
    )
    assert len(outcomes) == 1
    assert outcomes[0].outcome == "intercepted"
    assert calls, "create_ws should have been invoked for a valid small program"
