"""Tests for spec 005 US5 — secondary agent knowledge parity.

When the scheduler dispatches a continuous step, the secondary agent's
prompt MUST include:
  (a) the parent workspace's ``agents/<name>.md`` instructions file,
  (b) the task-specific ``data/continuous/<name>/plan.md``,
  (c) the task's current state (program fields, history).

These helpers live in ``bot/scheduler.py`` as pure functions — the full
``_handle_continuous_entries`` dispatch path is exercised end-to-end by
other suites; here we pin down the knowledge-parity contract.
"""

from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────────
# _load_parent_workspace_instructions
# ─────────────────────────────────────────────────────────────────────────


class TestLoadParentWorkspaceInstructions:
    def test_reads_existing_agent_file(self, tmp_path, monkeypatch):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ops.md").write_text(
            "# ops\n\nFollow the ops conventions from CLAUDE.md."
        )
        monkeypatch.setattr("config.AGENTS_DIR", agents_dir)

        import scheduler
        out = scheduler._load_parent_workspace_instructions(
            {"parent_workspace": "ops"}
        )
        assert "Follow the ops conventions" in out
        assert "# ops" in out

    def test_missing_file_returns_friendly_placeholder(
        self, tmp_path, monkeypatch,
    ):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        monkeypatch.setattr("config.AGENTS_DIR", agents_dir)

        import scheduler
        out = scheduler._load_parent_workspace_instructions(
            {"parent_workspace": "nonexistent"}
        )
        assert "not found" in out.lower()

    def test_empty_parent_returns_placeholder(self):
        import scheduler
        out = scheduler._load_parent_workspace_instructions({})
        assert "no parent workspace" in out.lower()

    def test_falls_back_to_legacy_field_name(self, tmp_path, monkeypatch):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "legacy-ws.md").write_text("legacy instructions")
        monkeypatch.setattr("config.AGENTS_DIR", agents_dir)

        import scheduler
        out = scheduler._load_parent_workspace_instructions(
            {"parent_workspace_name": "legacy-ws"}
        )
        assert "legacy instructions" in out


# ─────────────────────────────────────────────────────────────────────────
# _load_plan_md_for_prompt
# ─────────────────────────────────────────────────────────────────────────


class TestLoadPlanMd:
    def test_reads_plan_md_verbatim(self, tmp_path, monkeypatch):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
        import continuous as cont
        body = "# Plan: daily-report\n\n## Objective\nLorem ipsum\n"
        cont.write_plan_md("daily-report", body)

        import scheduler
        out = scheduler._load_plan_md_for_prompt("daily-report")
        assert "# Plan: daily-report" in out
        assert "Lorem ipsum" in out

    def test_missing_plan_returns_placeholder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")

        import scheduler
        out = scheduler._load_plan_md_for_prompt("never-created")
        assert "no plan.md" in out.lower()
        assert "program section" in out.lower()

    def test_does_not_raise_on_io_failures(self, tmp_path, monkeypatch):
        # Simulate read_plan_md raising.
        import continuous as cont

        def _raise(_name):
            raise OSError("disk gone")

        monkeypatch.setattr(cont, "read_plan_md", _raise)

        import scheduler
        out = scheduler._load_plan_md_for_prompt("any")
        # Must fall back gracefully.
        assert "no plan.md" in out.lower() or "not" in out.lower()


# ─────────────────────────────────────────────────────────────────────────
# Template has the US5 placeholders — prompt assembly contract
# ─────────────────────────────────────────────────────────────────────────


class TestTemplatePlaceholders:
    def test_continuous_step_template_has_parent_instructions_placeholder(self):
        from pathlib import Path
        template_path = (
            Path(__file__).resolve().parent.parent
            / "templates" / "CONTINUOUS_STEP.md"
        )
        text = template_path.read_text()
        assert "{{PARENT_WORKSPACE_INSTRUCTIONS}}" in text

    def test_continuous_step_template_has_plan_md_placeholder(self):
        from pathlib import Path
        template_path = (
            Path(__file__).resolve().parent.parent
            / "templates" / "CONTINUOUS_STEP.md"
        )
        text = template_path.read_text()
        assert "{{PLAN_MD}}" in text

    def test_continuous_step_template_preserves_existing_placeholders(self):
        from pathlib import Path
        template_path = (
            Path(__file__).resolve().parent.parent
            / "templates" / "CONTINUOUS_STEP.md"
        )
        text = template_path.read_text()
        for placeholder in (
            "{{OBJECTIVE}}",
            "{{SUCCESS_CRITERIA}}",
            "{{CONSTRAINTS}}",
            "{{STEP_NUMBER}}",
            "{{STEP_DESCRIPTION}}",
            "{{STATE_FILE}}",
            "{{LOG_FILE}}",
        ):
            assert placeholder in text, (
                "US5 refactor must preserve existing placeholder %s" % placeholder
            )

    def test_continuous_step_template_has_checkpoint_policy_placeholder(self):
        """0.24.0 — checkpoint_policy must be injected into the step prompt."""
        from pathlib import Path
        template_path = (
            Path(__file__).resolve().parent.parent
            / "templates" / "CONTINUOUS_STEP.md"
        )
        text = template_path.read_text()
        assert "{{CHECKPOINT_POLICY}}" in text
        # And the template must explain the four recognised values so the
        # step agent knows how to interpret each one.
        for policy in ("on-demand", "on-uncertainty", "on-milestone", "every-N-steps"):
            assert policy in text, (
                "CONTINUOUS_STEP.md must document policy %s" % policy
            )
