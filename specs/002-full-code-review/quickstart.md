# Quickstart: Reproducing Pass 2 Locally

Goal: let a reviewer pick any one module and complete a Pass 2 review in
a self-contained session, without re-reading the full plan.

## 1. Prerequisites

- Python 3.10+ with project venv active
- Working copy on branch `002-full-code-review`
- `pytest` passes (establishes baseline)

```bash
cd /Users/rpix/Workspace/products/robyx-ai
pytest tests/ -q
# Expected: 1086 passed (baseline)
```

If the baseline has drifted, note the new number in your session log and
treat that as the new floor — do not "fix" unrelated regressions.

## 2. Pick a module

Review Groups A–F are defined in `plan.md`. Pick one module. Examples:

- Group A, Security/Stability lens: `bot/scheduler.py`
- Group B, Natural-Interaction lens: `bot/messaging/telegram.py`
- Group E, Natural-Interaction lens: `bot/i18n.py`

One module per session is ideal — keep changes bisectable.

## 3. Apply the four-lens checklist

Walk the module top-to-bottom with the checklists from `plan.md`
(sections "Lens 1 — Security" … "Lens 4 — Natural interaction"). For the
Natural-Interaction lens, also apply the `contracts/conversation-contract.md`
checklist.

For every `send_message` / `reply` / `edit_message` call in the module, run
through the 8 questions in §8 of the conversation contract.

For every filesystem write, check: tmp+rename pattern? fsync before rename?
path validated against allow-list?

For every `subprocess.Popen` / `asyncio.create_subprocess_exec`: argv array?
env scrubbed? timeout enforced?

## 4. File findings

Append rows to `specs/002-full-code-review/findings.md` under
`## Pass 2 Findings` (create the section if it does not exist yet).

```markdown
## Pass 2 Findings

| ID | Module | Lens | Sev | Description | Fix |
|----|--------|------|-----|-------------|-----|
| P2-01 | scheduler.py | Stability | High | Wall-clock `time.time()` in retry-backoff can fire immediately on NTP jump | Switch to `time.monotonic()` for intervals |
| ... | ... | ... | ... | ... | ... |
```

Use the ID prefix `P2-` to distinguish from Pass 1 findings.

**Lens values**: `Security`, `Stability`, `UX` (ease of use), `NI` (natural
interaction).

## 5. Apply the fix

- Edit the module.
- Write a regression test that fails before the fix and passes after.
- If the fix touches a user-visible string, update `bot/i18n.py` for both
  IT and EN keys.
- If the fix crosses adapter boundaries, apply it consistently to all three
  (`telegram.py`, `discord.py`, `slack.py`) or document the parity gap.

## 6. Verify

```bash
pytest tests/ -q
# Expected: ≥ 1086 passed, with the new regression test included
```

If pytest shows fewer passes than baseline, diagnose before moving on.

## 7. Commit

Pass 2 follows the same commit-per-group cadence as Pass 1:

```
fix(code-review-p2): <lens> — <module> — <short description>

Finding P2-XX: <one-line why>

Test: tests/test_<module>.py::<test_name>
```

Keep commits small — one finding per commit where practical. Group commits
at the end of a review group (A, B, C, …) if many small findings pile up.

## 8. Close-out

When all modules across all six groups have been traversed:

1. Re-run `pytest tests/ -q` — must meet or exceed baseline.
2. Review `findings.md` — every Pass 2 row is `fixed` or `deferred with
   rationale`.
3. Re-evaluate Pass 1 deferred findings (F12, F13, F14, F17, F20, P1–P5) —
   each gets a Pass 2 status update: `fixed`, `still deferred`, or
   `rejected (not a real issue)`.
4. Update `VERSION` (patch bump if no schema change).
5. Create `releases/vX.Y.Z.md` with summary of Pass 2 changes.
6. Run `/speckit-git-commit` for the final commit; do NOT push without
   the user's explicit request.
