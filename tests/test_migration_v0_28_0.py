"""Spec 007 — migration v0_28_0 tests.

Covers:

- Empty / missing collaborative_workspaces.json → no-op + done marker.
- Pre-007 records (int chat_id, no platform) → normalised in place.
- Already-canonical records → no rewrite.
- Idempotent re-run (done marker present) → fast-path skip.
- Mixed file (some pre-007, some canonical) → only the legacy ones
  rewritten; the file ends up uniformly canonical.
- Atomic write contract: a temp file is not left behind on success.
- Invalid JSON aborts the chain (raises) so the operator sees the
  failure and the runner halts before the rest of 0.28 runs against
  unknown state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import pytest

from migrations import v0_28_0
from migrations.base import MigrationContext


def _ctx(tmp_path: Path) -> MigrationContext:
    return MigrationContext(platform=None, manager=None, data_dir=tmp_path)


def _legacy_record(name: str = "legacy", chat_id: int = -100123) -> dict:
    return {
        "id": "collab-%s" % name,
        "name": name,
        "display_name": name.title(),
        "agent_name": name,
        "chat_id": chat_id,  # int — legacy
        "interaction_mode": "intelligent",
        "status": "active",
        "created_at": 0,
        "created_by": 111,
        "roles": {"111": "owner"},
        # No platform / expected_platform keys.
    }


def _canonical_record(
    name: str = "canon", chat_id: str = "-100999", platform: str = "telegram",
) -> dict:
    return {
        "id": "collab-%s" % name,
        "name": name,
        "display_name": name.title(),
        "agent_name": name,
        "chat_id": chat_id,
        "platform": platform,
        "expected_platform": None,
        "interaction_mode": "intelligent",
        "status": "active",
        "created_at": 0,
        "created_by": 111,
        "roles": {"111": "owner"},
    }


def _seed(tmp_path: Path, records: dict[str, dict]) -> Path:
    collab = tmp_path / "collaborative_workspaces.json"
    collab.write_text(json.dumps(records, indent=2))
    return collab


# ── No-op cases ────────────────────────────────────────────────────────


async def test_missing_file_writes_done_marker(tmp_path):
    await v0_28_0.upgrade(_ctx(tmp_path))
    assert (tmp_path / "migrations" / "v0_28_0.done").exists()
    # No collab file was created (we never touch absent input).
    assert not (tmp_path / "collaborative_workspaces.json").exists()


async def test_done_marker_short_circuits(tmp_path):
    # Pre-create the done marker.
    (tmp_path / "migrations").mkdir(parents=True)
    (tmp_path / "migrations" / "v0_28_0.done").write_text("already-done\n")
    # Seed a file that WOULD need rewriting if the migration ran.
    collab = _seed(tmp_path, {"collab-x": _legacy_record("x", chat_id=-1)})
    before = collab.read_text()
    await v0_28_0.upgrade(_ctx(tmp_path))
    # File is unchanged because the done marker short-circuited the run.
    assert collab.read_text() == before


# ── Single-record normalisation ────────────────────────────────────────


async def test_coerces_int_chat_id_and_adds_platform(tmp_path):
    collab = _seed(tmp_path, {"collab-legacy": _legacy_record(chat_id=-100123)})
    await v0_28_0.upgrade(_ctx(tmp_path))
    data = json.loads(collab.read_text())
    rec = data["collab-legacy"]
    assert rec["platform"] == "telegram"
    assert rec["chat_id"] == "-100123"
    assert isinstance(rec["chat_id"], str)
    assert (tmp_path / "migrations" / "v0_28_0.done").exists()


async def test_canonical_record_unchanged(tmp_path):
    collab = _seed(tmp_path, {"collab-canon": _canonical_record()})
    before = collab.read_text()
    await v0_28_0.upgrade(_ctx(tmp_path))
    # Idempotent: no rewrite needed.
    assert collab.read_text() == before
    assert (tmp_path / "migrations" / "v0_28_0.done").exists()


# ── Idempotency ────────────────────────────────────────────────────────


async def test_idempotent_double_run(tmp_path):
    collab = _seed(tmp_path, {"collab-legacy": _legacy_record(chat_id=-100123)})
    await v0_28_0.upgrade(_ctx(tmp_path))
    after_first = collab.read_text()
    # Second run via done marker → no read, no write.
    await v0_28_0.upgrade(_ctx(tmp_path))
    assert collab.read_text() == after_first


async def test_idempotent_after_marker_removed(tmp_path):
    """Defense-in-depth: even if the done marker is missing, a
    fully-normalised file is re-recognised as canonical and skipped
    without rewriting."""
    collab = _seed(tmp_path, {"collab-canon": _canonical_record()})
    await v0_28_0.upgrade(_ctx(tmp_path))
    # Remove the done marker.
    (tmp_path / "migrations" / "v0_28_0.done").unlink()
    before = collab.read_text()
    await v0_28_0.upgrade(_ctx(tmp_path))
    assert collab.read_text() == before


# ── Mixed file ─────────────────────────────────────────────────────────


async def test_mixed_file_normalises_only_legacy(tmp_path):
    collab = _seed(tmp_path, {
        "collab-legacy": _legacy_record("legacy", chat_id=-100123),
        "collab-canon": _canonical_record("canon", chat_id="-100999"),
        "collab-discord": _canonical_record(
            "discord", chat_id="111:222", platform="discord",
        ),
    })
    await v0_28_0.upgrade(_ctx(tmp_path))
    data = json.loads(collab.read_text())
    # Legacy was rewritten.
    assert data["collab-legacy"]["chat_id"] == "-100123"
    assert data["collab-legacy"]["platform"] == "telegram"
    # Canonical record preserved verbatim (modulo dict ordering — values match).
    assert data["collab-canon"]["chat_id"] == "-100999"
    assert data["collab-canon"]["platform"] == "telegram"
    # Discord record preserved verbatim.
    assert data["collab-discord"]["chat_id"] == "111:222"
    assert data["collab-discord"]["platform"] == "discord"


# ── Atomic write contract ──────────────────────────────────────────────


async def test_no_temp_file_left_behind(tmp_path):
    collab = _seed(tmp_path, {"collab-legacy": _legacy_record(chat_id=-100)})
    await v0_28_0.upgrade(_ctx(tmp_path))
    # The temp-file pattern would have left "*.json.tmp" if os.replace
    # had failed. Successful path leaves only the canonical file.
    assert collab.exists()
    leftovers = list(tmp_path.glob("collaborative_workspaces.json.tmp"))
    assert leftovers == []


# ── Error handling ────────────────────────────────────────────────────


async def test_corrupt_json_raises(tmp_path):
    collab = tmp_path / "collaborative_workspaces.json"
    collab.write_text("{not valid json!!")
    with pytest.raises(json.JSONDecodeError):
        await v0_28_0.upgrade(_ctx(tmp_path))
    # Done marker NOT written on failure — the chain halts.
    assert not (tmp_path / "migrations" / "v0_28_0.done").exists()


async def test_unexpected_top_level_shape_raises(tmp_path):
    collab = tmp_path / "collaborative_workspaces.json"
    collab.write_text(json.dumps(["not", "a", "dict"]))
    with pytest.raises(RuntimeError, match="unexpected top-level shape"):
        await v0_28_0.upgrade(_ctx(tmp_path))


# ── Migration metadata ────────────────────────────────────────────────


def test_migration_constant_well_formed():
    assert v0_28_0.MIGRATION.from_version == "0.27.2"
    assert v0_28_0.MIGRATION.to_version == "0.28.0"
    assert v0_28_0.MIGRATION.upgrade is v0_28_0.upgrade


# ── Synthetic 50-record scale test ─────────────────────────────────────


async def test_scale_50_records(tmp_path):
    """SC-005: migration runs in <1s on a typical install. We don't
    measure wall-clock here (CI variance), but we do verify correctness
    on a 50-record mix to catch any per-record edge case."""
    records = {}
    for i in range(25):
        records["collab-l%d" % i] = _legacy_record("l%d" % i, chat_id=-1000 - i)
    for i in range(25):
        records["collab-c%d" % i] = _canonical_record("c%d" % i, chat_id=str(-2000 - i))
    collab = _seed(tmp_path, records)
    await v0_28_0.upgrade(_ctx(tmp_path))
    data = json.loads(collab.read_text())
    assert len(data) == 50
    # Every record is in canonical form post-migration.
    for rec in data.values():
        assert isinstance(rec["chat_id"], str)
        assert rec["platform"] == "telegram"
