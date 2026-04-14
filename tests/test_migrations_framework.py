"""Unit tests for the version-chained migration framework
(``bot/migrations/base.py``, ``runner.py``, ``tracker.py``).

Framework + contract tests only. Behaviour of individual migrations
lives in their own test modules next to each ``vX_Y_Z.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# version_tuple
# ---------------------------------------------------------------------------


class TestVersionTuple:
    def test_parses_three_part_version(self):
        from migrations.base import version_tuple

        assert version_tuple("0.20.12") == (0, 20, 12)

    def test_parses_arbitrary_depth(self):
        from migrations.base import version_tuple

        assert version_tuple("1.2.3.4") == (1, 2, 3, 4)

    def test_raises_on_malformed(self):
        from migrations.base import version_tuple

        with pytest.raises(ValueError):
            version_tuple("0.20.x")

    def test_comparison_is_numeric_not_lexicographic(self):
        """"0.20.10" must be greater than "0.20.9" — a lexicographic sort
        would get this wrong and silently skip intermediate migrations."""
        from migrations.base import version_tuple

        assert version_tuple("0.20.10") > version_tuple("0.20.9")


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class TestTracker:
    def test_load_missing_returns_empty(self, data_dir):
        from migrations.tracker import load

        assert load(data_dir) == {}

    def test_load_corrupt_returns_empty(self, data_dir):
        from migrations.tracker import load

        (data_dir / "migrations.json").write_text("{not json")
        assert load(data_dir) == {}

    def test_get_chain_state_seeds_when_missing(self, data_dir):
        from migrations.tracker import SEED_VERSION, get_chain_state

        tracker: dict = {}
        chain = get_chain_state(tracker)
        assert chain["current_version"] == SEED_VERSION
        assert chain["history"] == []
        # Mutation must persist in the caller's dict.
        assert tracker["_chain_"] is chain

    def test_record_step_advances_on_ok(self, data_dir):
        from migrations.tracker import current_version, record_step

        tracker: dict = {}
        record_step(tracker, "0.20.11", "0.20.12", "ok")
        assert current_version(tracker) == "0.20.12"
        assert tracker["_chain_"]["history"][0]["status"] == "ok"

    def test_record_step_does_not_advance_on_error(self, data_dir):
        from migrations.tracker import current_version, record_step

        tracker: dict = {}
        record_step(tracker, "0.20.11", "0.20.12", "error", "boom")
        assert current_version(tracker) == "0.20.11"
        assert tracker["_chain_"]["history"][0]["error"] == "boom"

    def test_save_and_reload_round_trip(self, data_dir):
        from migrations.tracker import load, record_step, save

        tracker = load(data_dir)
        record_step(tracker, "0.20.11", "0.20.12", "ok")
        save(data_dir, tracker)
        reloaded = load(data_dir)
        assert reloaded["_chain_"]["current_version"] == "0.20.12"


# ---------------------------------------------------------------------------
# Runner: discovery + validation
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_discovers_production_migrations(self):
        from migrations.runner import discover

        migrations = discover("migrations")
        assert migrations, "expected at least one MIGRATION module"
        assert all(hasattr(m, "from_version") for m in migrations)

    def test_migrations_are_sorted_by_to_version(self):
        from migrations.base import version_tuple
        from migrations.runner import discover

        migrations = discover("migrations")
        tos = [version_tuple(m.to_version) for m in migrations]
        assert tos == sorted(tos)


class TestValidateChain:
    def test_accepts_continuous_chain(self):
        from migrations.base import Migration
        from migrations.runner import validate_chain

        async def noop(ctx):
            return None

        chain = [
            Migration("0.1.0", "0.2.0", "a", noop),
            Migration("0.2.0", "0.3.0", "b", noop),
        ]
        validate_chain(chain)  # should not raise

    def test_rejects_gap(self):
        from migrations.base import Migration
        from migrations.runner import validate_chain

        async def noop(ctx):
            return None

        chain = [
            Migration("0.1.0", "0.2.0", "a", noop),
            Migration("0.2.5", "0.3.0", "b", noop),
        ]
        with pytest.raises(ValueError, match="chain gap"):
            validate_chain(chain)

    def test_rejects_duplicate_to_version(self):
        from migrations.base import Migration
        from migrations.runner import validate_chain

        async def noop(ctx):
            return None

        chain = [
            Migration("0.1.0", "0.2.0", "a", noop),
            Migration("0.2.0", "0.2.0", "b", noop),
        ]
        with pytest.raises(ValueError, match="duplicate"):
            validate_chain(chain)


# ---------------------------------------------------------------------------
# Runner: chain execution
# ---------------------------------------------------------------------------


class TestRunChain:
    @pytest.mark.asyncio
    async def test_runs_pending_in_order_and_advances_tracker(self, data_dir, monkeypatch):
        """A two-step chain must run both steps, in order, and advance
        ``current_version`` after each."""
        from migrations.base import Migration, MigrationContext
        from migrations.runner import discover, run_chain
        from migrations.tracker import current_version, load

        calls: list[str] = []

        async def up_a(ctx):
            calls.append("a")

        async def up_b(ctx):
            calls.append("b")

        chain = [
            Migration("9.0.0", "9.1.0", "first", up_a),
            Migration("9.1.0", "9.2.0", "second", up_b),
        ]

        # Seed tracker at the chain's from_version so both steps are pending.
        (data_dir / "migrations.json").write_text(json.dumps({
            "_chain_": {"current_version": "9.0.0", "history": []},
        }))

        monkeypatch.setattr("migrations.runner.discover", lambda *_: chain)
        ctx = MigrationContext(data_dir=data_dir)
        summary = await run_chain(ctx, data_dir, package_name="migrations")

        assert calls == ["a", "b"]
        assert summary == [("9.0.0→9.1.0", "ok"), ("9.1.0→9.2.0", "ok")]
        assert current_version(load(data_dir)) == "9.2.0"

    @pytest.mark.asyncio
    async def test_stops_on_first_error(self, data_dir, monkeypatch):
        from migrations.base import Migration, MigrationContext
        from migrations.runner import run_chain
        from migrations.tracker import current_version, load

        calls: list[str] = []

        async def up_a(ctx):
            calls.append("a")

        async def up_b(ctx):
            calls.append("b")
            raise RuntimeError("boom")

        async def up_c(ctx):
            calls.append("c")  # must NEVER run after b failed

        chain = [
            Migration("9.0.0", "9.1.0", "first", up_a),
            Migration("9.1.0", "9.2.0", "second", up_b),
            Migration("9.2.0", "9.3.0", "third", up_c),
        ]

        (data_dir / "migrations.json").write_text(json.dumps({
            "_chain_": {"current_version": "9.0.0", "history": []},
        }))
        monkeypatch.setattr("migrations.runner.discover", lambda *_: chain)

        ctx = MigrationContext(data_dir=data_dir)
        summary = await run_chain(ctx, data_dir, package_name="migrations")

        assert calls == ["a", "b"]
        assert summary == [("9.0.0→9.1.0", "ok"), ("9.1.0→9.2.0", "error")]
        assert current_version(load(data_dir)) == "9.1.0"

    @pytest.mark.asyncio
    async def test_skips_already_applied(self, data_dir, monkeypatch):
        from migrations.base import Migration, MigrationContext
        from migrations.runner import run_chain

        async def up(ctx):  # pragma: no cover - must not be called
            raise AssertionError("upgrade called on an up-to-date chain")

        chain = [Migration("9.0.0", "9.1.0", "first", up)]
        (data_dir / "migrations.json").write_text(json.dumps({
            "_chain_": {"current_version": "9.1.0", "history": []},
        }))
        monkeypatch.setattr("migrations.runner.discover", lambda *_: chain)

        ctx = MigrationContext(data_dir=data_dir)
        summary = await run_chain(ctx, data_dir, package_name="migrations")

        assert summary == []

    @pytest.mark.asyncio
    async def test_multi_version_jump_runs_every_intermediate(self, data_dir, monkeypatch):
        """The whole point of the framework: jumping 0.18 → 0.21 must run
        every intermediate step, not just the newest one."""
        from migrations.base import Migration, MigrationContext
        from migrations.runner import run_chain

        called: list[str] = []

        def step(from_v, to_v):
            async def up(ctx):
                called.append(to_v)
            return Migration(from_v, to_v, "step %s" % to_v, up)

        chain = [
            step("0.18.0", "0.19.0"),
            step("0.19.0", "0.20.0"),
            step("0.20.0", "0.21.0"),
        ]
        (data_dir / "migrations.json").write_text(json.dumps({
            "_chain_": {"current_version": "0.18.0", "history": []},
        }))
        monkeypatch.setattr("migrations.runner.discover", lambda *_: chain)

        ctx = MigrationContext(data_dir=data_dir)
        await run_chain(ctx, data_dir, package_name="migrations")

        assert called == ["0.19.0", "0.20.0", "0.21.0"]


# ---------------------------------------------------------------------------
# Contract: every release >= 0.20.12 has a matching vX_Y_Z.py migration,
# and the combined chain is continuous from SEED_VERSION up to the
# repo's current VERSION.
# ---------------------------------------------------------------------------


class TestChainContract:
    def test_every_release_since_0_20_12_has_a_migration_module(self):
        """For every ``releases/X.Y.Z.md`` with Z >= 12 at minor 20, there
        must exist a matching ``bot/migrations/vX_Y_Z.py`` module. The
        chain has no exceptions — a release with no data changes still
        ships a no-op migration."""
        from migrations.base import version_tuple

        repo_root = Path(__file__).resolve().parents[1]
        releases_dir = repo_root / "releases"
        migrations_dir = repo_root / "bot" / "migrations"

        version_pattern = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.md$")
        release_versions = []
        for f in releases_dir.iterdir():
            m = version_pattern.match(f.name)
            if m:
                release_versions.append(f.stem)

        # Framework introduced in 0.20.12; earlier releases are legacy.
        eligible = [v for v in release_versions
                    if version_tuple(v) >= version_tuple("0.20.12")]

        missing: list[str] = []
        for v in eligible:
            expected = migrations_dir / ("v" + v.replace(".", "_") + ".py")
            if not expected.exists():
                missing.append(v)

        assert not missing, (
            "Missing migration modules for: %s. Every release >= 0.20.12 "
            "must ship a vX_Y_Z.py file (no-op is fine). Use "
            "`python scripts/new_migration.py X.Y.Z` to scaffold." % missing
        )

    def test_chain_is_continuous_and_reaches_current_version(self):
        """The chain starts at SEED_VERSION (0.20.11) and must reach the
        version in ``VERSION`` without any gaps."""
        from migrations.runner import discover, validate_chain
        from migrations.tracker import SEED_VERSION

        repo_root = Path(__file__).resolve().parents[1]
        current = (repo_root / "VERSION").read_text().strip()

        chain = discover("migrations")
        validate_chain(chain)

        assert chain, "chain must contain at least the bootstrap migration"
        assert chain[0].from_version == SEED_VERSION, (
            "chain must start at SEED_VERSION=%s, got %s"
            % (SEED_VERSION, chain[0].from_version)
        )
        assert chain[-1].to_version == current, (
            "chain must reach current VERSION=%s, got %s"
            % (current, chain[-1].to_version)
        )
