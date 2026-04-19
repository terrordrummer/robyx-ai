"""Tests for bot.topics — channel/topic management via Platform abstraction."""

import json
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, patch

import topics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_platform():
    """A mock Platform with async methods for channel operations."""
    p = AsyncMock()
    p.create_channel = AsyncMock(return_value=999)
    p.close_channel = AsyncMock(return_value=True)
    p.send_to_channel = AsyncMock(return_value=True)
    return p


@pytest.fixture(autouse=True)
def _patch_topics_paths(tmp_path, monkeypatch):
    """Patch all path constants inside the topics module to use tmp_path/data/."""
    data = tmp_path / "data"
    monkeypatch.setattr(topics, "AGENTS_DIR", data / "agents")
    monkeypatch.setattr(topics, "SPECIALISTS_DIR", data / "specialists")
    monkeypatch.setattr(topics, "SPECIALISTS_FILE", data / "specialists.md")
    monkeypatch.setattr(topics, "DATA_DIR", data)
    monkeypatch.setattr(topics, "_cancel_tasks_for_agent_file", lambda *args, **kwargs: 0)
    # Patch scheduler queue file for tests that use add_task
    import scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "QUEUE_FILE", data / "queue.json")
    data.mkdir(exist_ok=True)
    (data / "agents").mkdir(exist_ok=True)
    (data / "specialists").mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# _sanitize_task_name
# ---------------------------------------------------------------------------

class TestSanitizeTaskName:
    def test_basic_spaces(self):
        assert topics._sanitize_task_name("My Project") == "my-project"

    def test_special_characters(self):
        result = topics._sanitize_task_name("Hello World!")
        # '!' -> '-', but strip('-') removes trailing hyphen
        assert "hello" in result
        assert "world" in result

    def test_leading_trailing_whitespace(self):
        assert topics._sanitize_task_name("  Test  ") == "test"

    def test_already_clean(self):
        assert topics._sanitize_task_name("clean-name") == "clean-name"

    def test_mixed_case_and_symbols(self):
        result = topics._sanitize_task_name("ML_Finance 2.0!")
        assert result.startswith("ml-finance")
        assert result == result.lower()

    def test_empty_after_strip(self):
        result = topics._sanitize_task_name("!!!")
        assert result == ""


# ---------------------------------------------------------------------------
# create_workspace (full flow)
# ---------------------------------------------------------------------------

class TestCreateWorkspace:
    @pytest.mark.asyncio
    async def test_success_full_flow(self, tmp_path, agent_manager, mock_platform):
        """Channel created -> agent file written -> queue.json updated -> agent registered -> welcome sent."""
        mock_platform.create_channel = AsyncMock(return_value=500)

        result = await topics.create_workspace(
            name="My Workspace",
            task_type="scheduled",
            frequency="daily",
            model="claude-sonnet-4-20250514",
            scheduled_at="08:00",
            instructions="Do the thing.",
            manager=agent_manager,
            work_dir=str(tmp_path / "workspace"),
            platform=mock_platform,
        )

        assert result is not None
        assert result["name"] == "my-workspace"
        assert result["display_name"] == "My Workspace"
        assert result["thread_id"] == 500
        assert result["type"] == "scheduled"

        # Agent file written
        agent_file = tmp_path / "data" / "agents" / "my-workspace.md"
        assert agent_file.exists()
        content = agent_file.read_text()
        assert "# My Workspace" in content
        assert "Do the thing." in content

        # queue.json entry created
        queue_file = tmp_path / "data" / "queue.json"
        assert queue_file.exists()
        queue = json.loads(queue_file.read_text())
        entry = next(e for e in queue if e["name"] == "my-workspace")
        assert entry["type"] == "periodic"
        assert entry["thread_id"] == "500"

        # Data dir created
        assert (tmp_path / "data" / "my-workspace").is_dir()

        # Agent registered in manager
        agent = agent_manager.get("my-workspace")
        assert agent is not None
        assert agent.thread_id == 500
        assert agent.agent_type == "workspace"

        # Welcome message sent via platform
        mock_platform.send_to_channel.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_topic_creation_fails(self, tmp_path, agent_manager, mock_platform):
        mock_platform.create_channel = AsyncMock(return_value=None)

        result = await topics.create_workspace(
            name="Fail",
            task_type="test",
            frequency="none",
            model="claude-sonnet-4-20250514",
            scheduled_at="none",
            instructions="Nope",
            manager=agent_manager,
            work_dir=str(tmp_path),
            platform=mock_platform,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_interactive_workspace_writes_no_queue_entry(self, tmp_path, agent_manager, mock_platform):
        """Interactive workspaces are agent-only — no entry in queue.json."""
        mock_platform.create_channel = AsyncMock(return_value=600)

        await topics.create_workspace(
            name="NoFreq",
            task_type="interactive",
            frequency="none",
            model="m",
            scheduled_at="none",
            instructions="x",
            manager=agent_manager,
            work_dir=str(tmp_path),
            platform=mock_platform,
        )

        queue_file = tmp_path / "data" / "queue.json"
        if queue_file.exists():
            queue = json.loads(queue_file.read_text())
            assert not any(e["name"] == "nofreq" for e in queue)

    @pytest.mark.asyncio
    async def test_agent_file_content(self, tmp_path, agent_manager, mock_platform):
        """Verify the agent instructions file has the expected format."""
        mock_platform.create_channel = AsyncMock(return_value=501)

        await topics.create_workspace(
            name="Agent Check",
            task_type="test",
            frequency="daily",
            model="m",
            scheduled_at="08:00",
            instructions="  Trimmed instructions  ",
            manager=agent_manager,
            work_dir=str(tmp_path),
            platform=mock_platform,
        )

        agent_file = tmp_path / "data" / "agents" / "agent-check.md"
        content = agent_file.read_text()
        assert content.startswith("# Agent Check\n")
        assert "Trimmed instructions" in content
        # Instructions are stripped
        assert "  Trimmed instructions  " not in content

    @pytest.mark.asyncio
    @patch.object(topics, "_add_task")
    async def test_one_shot_requires_scheduled_at_before_side_effects(
        self, mock_add_task, tmp_path, agent_manager, mock_platform
    ):
        with pytest.raises(
            ValueError, match="scheduled_at is required for one-shot workspaces"
        ):
            await topics.create_workspace(
                name="Missing Time",
                task_type="one-shot",
                frequency="none",
                model="m",
                scheduled_at="none",
                instructions="x",
                manager=agent_manager,
                work_dir=str(tmp_path),
                platform=mock_platform,
            )

        mock_platform.create_channel.assert_not_awaited()
        mock_add_task.assert_not_called()
        assert not (tmp_path / "data" / "agents" / "missing-time.md").exists()
        assert agent_manager.get("missing-time") is None

    @pytest.mark.asyncio
    @patch.object(topics, "_add_task")
    async def test_one_shot_rejects_malformed_scheduled_at_before_side_effects(
        self, mock_add_task, tmp_path, agent_manager, mock_platform
    ):
        with pytest.raises(
            ValueError,
            match="scheduled_at for one-shot workspaces must be a valid ISO datetime",
        ):
            await topics.create_workspace(
                name="Bad Time",
                task_type="one-shot",
                frequency="none",
                model="m",
                scheduled_at="tomorrow",
                instructions="x",
                manager=agent_manager,
                work_dir=str(tmp_path),
                platform=mock_platform,
            )

        mock_platform.create_channel.assert_not_awaited()
        mock_add_task.assert_not_called()
        assert not (tmp_path / "data" / "agents" / "bad-time.md").exists()

    @pytest.mark.asyncio
    @patch.object(topics, "_add_task")
    async def test_one_shot_normalizes_scheduled_at_before_queue_write(
        self, mock_add_task, tmp_path, agent_manager, mock_platform
    ):
        mock_platform.create_channel = AsyncMock(return_value=502)

        await topics.create_workspace(
            name="Timed Once",
            task_type="one-shot",
            frequency="none",
            model="m",
            scheduled_at="2099-06-01T12:00:00",
            instructions="x",
            manager=agent_manager,
            work_dir=str(tmp_path),
            platform=mock_platform,
        )

        queued_task = mock_add_task.call_args.args[0]
        assert queued_task["scheduled_at"] == "2099-06-01T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Reserved / duplicate name guard
# ---------------------------------------------------------------------------


class TestReservedAndDuplicateNames:
    """Regression guard for M3: creating a workspace or specialist whose
    sanitized name collides with a reserved name or an already-registered
    agent must be rejected *before* any side effect (no channel, no file,
    no queue entry). The failure surfaces as a ``ValueError`` so the
    handler can show the user a specific reason instead of a generic
    'failed to create' message."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("display_name", ["robyx", "Robyx", "ROBYX", "orchestrator"])
    async def test_workspace_rejects_reserved_name(
        self, display_name, tmp_path, agent_manager, mock_platform
    ):
        with pytest.raises(ValueError, match="reserved"):
            await topics.create_workspace(
                name=display_name,
                task_type="interactive",
                frequency="none",
                model="m",
                scheduled_at="none",
                instructions="nope",
                manager=agent_manager,
                work_dir=str(tmp_path),
                platform=mock_platform,
            )
        # No side effects — channel not even requested.
        mock_platform.create_channel.assert_not_awaited()
        assert not (tmp_path / "data" / "agents" / "robyx.md").exists()

    @pytest.mark.asyncio
    async def test_workspace_rejects_empty_sanitized_name(
        self, tmp_path, agent_manager, mock_platform
    ):
        # "!!!" sanitizes to "" — must be refused.
        with pytest.raises(ValueError, match="reserved"):
            await topics.create_workspace(
                name="!!!",
                task_type="interactive",
                frequency="none",
                model="m",
                scheduled_at="none",
                instructions="x",
                manager=agent_manager,
                work_dir=str(tmp_path),
                platform=mock_platform,
            )
        mock_platform.create_channel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_workspace_rejects_duplicate_name(
        self, tmp_path, agent_manager, mock_platform
    ):
        mock_platform.create_channel = AsyncMock(return_value=500)
        await topics.create_workspace(
            name="My Thing", task_type="interactive", frequency="none",
            model="m", scheduled_at="none", instructions="first",
            manager=agent_manager, work_dir=str(tmp_path), platform=mock_platform,
        )

        with pytest.raises(ValueError, match="already in use"):
            await topics.create_workspace(
                name="my-thing",  # same sanitized form
                task_type="interactive", frequency="none",
                model="m", scheduled_at="none", instructions="second",
                manager=agent_manager, work_dir=str(tmp_path), platform=mock_platform,
            )
        # Only the first create_channel call happened.
        assert mock_platform.create_channel.await_count == 1

    @pytest.mark.asyncio
    async def test_specialist_rejects_reserved_name(
        self, tmp_path, agent_manager, mock_platform
    ):
        with pytest.raises(ValueError, match="reserved"):
            await topics.create_specialist(
                name="Robyx",
                model="m",
                instructions="nope",
                manager=agent_manager,
                work_dir=str(tmp_path),
                platform=mock_platform,
            )
        mock_platform.create_channel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_specialist_rejects_duplicate_of_workspace(
        self, tmp_path, agent_manager, mock_platform
    ):
        """A workspace and a specialist cannot share a name — they live in
        the same AgentManager namespace."""
        mock_platform.create_channel = AsyncMock(return_value=500)
        await topics.create_workspace(
            name="Review", task_type="interactive", frequency="none",
            model="m", scheduled_at="none", instructions="x",
            manager=agent_manager, work_dir=str(tmp_path), platform=mock_platform,
        )
        with pytest.raises(ValueError, match="already in use"):
            await topics.create_specialist(
                name="Review", model="m", instructions="y",
                manager=agent_manager, work_dir=str(tmp_path), platform=mock_platform,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("display_name", ["Bad | Name", "Bad\nName"])
    async def test_workspace_rejects_table_breaking_display_name(
        self, display_name, tmp_path, agent_manager, mock_platform
    ):
        with pytest.raises(ValueError, match="unsupported table characters"):
            await topics.create_workspace(
                name=display_name,
                task_type="interactive",
                frequency="none",
                model="m",
                scheduled_at="none",
                instructions="x",
                manager=agent_manager,
                work_dir=str(tmp_path),
                platform=mock_platform,
            )

        mock_platform.create_channel.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("display_name", ["Bad | Specialist", "Bad\nSpecialist"])
    async def test_specialist_rejects_table_breaking_display_name(
        self, display_name, tmp_path, agent_manager, mock_platform
    ):
        with pytest.raises(ValueError, match="unsupported table characters"):
            await topics.create_specialist(
                name=display_name,
                model="m",
                instructions="x",
                manager=agent_manager,
                work_dir=str(tmp_path),
                platform=mock_platform,
            )

        mock_platform.create_channel.assert_not_awaited()
        assert not (tmp_path / "data" / "specialists.md").exists()


# ---------------------------------------------------------------------------
# close_workspace
# ---------------------------------------------------------------------------

class TestCloseWorkspace:
    @pytest.mark.asyncio
    @patch.object(topics, "_cancel_tasks_for_agent_file", return_value=2)
    async def test_success(self, mock_cancel, tmp_path, agent_manager, mock_platform):
        # Set up: create a workspace first
        mock_platform.create_channel = AsyncMock(return_value=700)

        await topics.create_workspace(
            name="ToClose",
            task_type="scheduled",
            frequency="daily",
            model="m",
            scheduled_at="09:00",
            instructions="close me",
            manager=agent_manager,
            work_dir=str(tmp_path),
            platform=mock_platform,
        )

        # Now close it
        result = await topics.close_workspace("toclose", agent_manager, platform=mock_platform)

        assert result is True

        mock_cancel.assert_called_once_with(
            "agents/toclose.md",
            reason="workspace closed",
        )

        # Agent removed from manager
        assert agent_manager.get("toclose") is None

    @pytest.mark.asyncio
    async def test_agent_not_found(self, agent_manager, mock_platform):
        result = await topics.close_workspace("nonexistent", agent_manager, platform=mock_platform)
        assert result is False

    @pytest.mark.asyncio
    async def test_agent_no_thread_id(self, tmp_path, agent_manager, mock_platform):
        """Agent exists but has no thread_id -- should still succeed, skipping topic ops."""
        agent_manager.add_agent(
            name="no-thread",
            work_dir=str(tmp_path),
            description="test",
            agent_type="workspace",
            thread_id=None,
        )
        # No platform calls expected since thread_id is None
        result = await topics.close_workspace("no-thread", agent_manager, platform=mock_platform)
        assert result is True
        assert agent_manager.get("no-thread") is None

    @pytest.mark.asyncio
    @patch.object(topics, "_cancel_tasks_for_agent_file", return_value=1)
    async def test_cancels_pending_tasks_for_workspace(
        self, mock_cancel, tmp_path, agent_manager, mock_platform
    ):
        agent_manager.add_agent(
            name="queued-workspace",
            work_dir=str(tmp_path),
            description="queued workspace",
            agent_type="workspace",
            thread_id=900,
        )

        result = await topics.close_workspace(
            "queued-workspace", agent_manager, platform=mock_platform,
        )

        assert result is True
        mock_cancel.assert_called_once_with(
            "agents/queued-workspace.md",
            reason="workspace closed",
        )

    @pytest.mark.asyncio
    async def test_close_workspace_cancels_real_queue_entries(
        self, tmp_path, agent_manager, mock_platform, monkeypatch
    ):
        import scheduler as sched_mod

        queue_file = tmp_path / "data" / "queue.json"
        monkeypatch.setattr(sched_mod, "QUEUE_FILE", queue_file)
        monkeypatch.setattr(topics, "_cancel_tasks_for_agent_file", sched_mod.cancel_tasks_for_agent_file)

        agent_manager.add_agent(
            name="integrated",
            work_dir=str(tmp_path),
            description="integrated workspace",
            agent_type="workspace",
            thread_id=901,
        )
        queue_file.write_text(json.dumps([
            {
                "id": "one-shot",
                "name": "integrated-once",
                "agent_file": "agents/integrated.md",
                "type": "one-shot",
                "scheduled_at": "2099-01-01T00:00:00+00:00",
                "status": "pending",
            },
            {
                "id": "periodic",
                "name": "integrated-periodic",
                "agent_file": "agents/integrated.md",
                "type": "periodic",
                "next_run": "2099-01-01T00:00:00+00:00",
                "status": "pending",
            },
            {
                "id": "other",
                "name": "other-workspace",
                "agent_file": "agents/other.md",
                "type": "one-shot",
                "scheduled_at": "2099-01-01T00:00:00+00:00",
                "status": "pending",
            },
        ]))

        result = await topics.close_workspace("integrated", agent_manager, platform=mock_platform)

        assert result is True
        assert agent_manager.get("integrated") is None

        queued = {task["id"]: task for task in json.loads(queue_file.read_text())}
        assert queued["one-shot"]["status"] == "canceled"
        assert queued["one-shot"]["canceled_reason"] == "workspace closed"
        assert queued["periodic"]["status"] == "canceled"
        assert queued["other"]["status"] == "pending"


# ---------------------------------------------------------------------------
# create_specialist
# ---------------------------------------------------------------------------

class TestCreateSpecialist:
    @pytest.mark.asyncio
    async def test_success(self, tmp_path, agent_manager, mock_platform):
        mock_platform.create_channel = AsyncMock(return_value=800)

        result = await topics.create_specialist(
            name="Code Reviewer",
            model="claude-sonnet-4-20250514",
            instructions="Review code carefully.",
            manager=agent_manager,
            work_dir=str(tmp_path),
            platform=mock_platform,
        )

        assert result is not None
        assert result["name"] == "code-reviewer"
        assert result["display_name"] == "Code Reviewer"
        assert result["thread_id"] == 800

        # Specialist file written
        spec_file = tmp_path / "data" / "specialists" / "code-reviewer.md"
        assert spec_file.exists()
        content = spec_file.read_text()
        assert "Cross-functional Specialist" in content
        assert "Review code carefully." in content

        # specialists.md updated
        spec_md = tmp_path / "data" / "specialists.md"
        assert spec_md.exists()
        spec_text = spec_md.read_text()
        assert "| code-reviewer |" in spec_text
        assert "| 800 |" in spec_text

        # Agent registered as specialist
        agent = agent_manager.get("code-reviewer")
        assert agent is not None
        assert agent.agent_type == "specialist"
        assert agent.thread_id == 800

    @pytest.mark.asyncio
    async def test_topic_fails(self, tmp_path, agent_manager, mock_platform):
        mock_platform.create_channel = AsyncMock(return_value=None)

        result = await topics.create_specialist(
            name="Bad",
            model="m",
            instructions="x",
            manager=agent_manager,
            work_dir=str(tmp_path),
            platform=mock_platform,
        )
        assert result is None



# ---------------------------------------------------------------------------
# _append_to_specialists
# ---------------------------------------------------------------------------

class TestAppendToSpecialists:
    def test_file_does_not_exist(self, tmp_path):
        spec_file = tmp_path / "data" / "specialists.md"
        if spec_file.exists():
            spec_file.unlink()

        row = "| spec | specialists/spec.md | m | 1 | Spec |\n"
        topics._append_to_specialists(row)

        assert spec_file.exists()
        text = spec_file.read_text()
        assert "| Agent |" in text
        assert "| spec |" in text

    def test_file_exists_appends(self, tmp_path):
        spec_file = tmp_path / "data" / "specialists.md"
        header = (
            "| Agent | Instructions | Model | Thread ID | Description |\n"
            "|-------|-------------|-------|-----------|-------------|\n"
            "| old | specialists/old.md | m | 1 | Old |\n"
        )
        spec_file.write_text(header)

        row = "| new | specialists/new.md | m | 2 | New |\n"
        topics._append_to_specialists(row)

        text = spec_file.read_text()
        assert "| old |" in text
        assert "| new |" in text



# ---------------------------------------------------------------------------
# create_workspace / create_specialist propagate the ``model`` preference
# ---------------------------------------------------------------------------


class TestCreateWorkspacePersistsModel:
    """Workspaces and specialists must record their preferred model on the
    in-memory Agent so :func:`model_preferences.resolve_model_preference`
    can resolve it later, even when no model is supplied at invocation."""

    @pytest.mark.asyncio
    async def test_workspace_stores_model_on_agent(
        self, tmp_path, agent_manager, mock_platform
    ):
        mock_platform.create_channel = AsyncMock(return_value=801)

        await topics.create_workspace(
            name="With Model",
            task_type="interactive",
            frequency="none",
            model="powerful",
            scheduled_at="none",
            instructions="x",
            manager=agent_manager,
            work_dir=str(tmp_path),
            platform=mock_platform,
        )

        agent = agent_manager.get("with-model")
        assert agent is not None
        assert agent.model == "powerful"

    @pytest.mark.asyncio
    async def test_specialist_stores_model_on_agent(
        self, tmp_path, agent_manager, mock_platform
    ):
        mock_platform.create_channel = AsyncMock(return_value=802)

        await topics.create_specialist(
            name="Reviewer",
            model="balanced",
            instructions="be terse",
            manager=agent_manager,
            work_dir=str(tmp_path),
            platform=mock_platform,
        )

        agent = agent_manager.get("reviewer")
        assert agent is not None
        assert agent.model == "balanced"
        assert agent.agent_type == "specialist"


# ---------------------------------------------------------------------------
# _update_table_thread_id helpers
# ---------------------------------------------------------------------------


class TestUpdateTableThreadId:
    def test_rewrites_thread_id_in_queue_json(self, tmp_path):
        queue_file = tmp_path / "data" / "queue.json"
        queue_file.write_text(json.dumps([
            {"name": "alpha", "agent_file": "agents/alpha.md", "type": "periodic",
             "thread_id": "", "status": "pending"},
            {"name": "beta", "agent_file": "agents/beta.md", "type": "periodic",
             "thread_id": "9", "status": "pending"},
        ]))

        topics._update_queue_entry_thread_id("alpha", 123)

        queue = json.loads(queue_file.read_text())
        alpha = next(e for e in queue if e["name"] == "alpha")
        beta = next(e for e in queue if e["name"] == "beta")
        assert alpha["thread_id"] == "123"
        assert beta["thread_id"] == "9"

    def test_clearing_thread_id_writes_empty(self, tmp_path):
        queue_file = tmp_path / "data" / "queue.json"
        queue_file.write_text(json.dumps([
            {"name": "rev", "agent_file": "agents/rev.md", "type": "periodic",
             "thread_id": "7", "status": "pending"},
        ]))

        topics._update_queue_entry_thread_id("rev", None)

        queue = json.loads(queue_file.read_text())
        assert queue[0]["thread_id"] == ""

    def test_missing_queue_file_is_a_noop(self, tmp_path):
        topics._update_queue_entry_thread_id("anything", 1)  # must not raise


# ---------------------------------------------------------------------------
# heal_detached_workspaces
# ---------------------------------------------------------------------------


class TestHealDetachedWorkspaces:
    """When a Telegram restart leaves a workspace with ``Thread ID = -``,
    booting must transparently re-create the topic and persist the new id."""

    @pytest.mark.asyncio
    async def test_heals_detached_workspace_and_updates_queue_json(
        self, tmp_path, agent_manager, mock_platform
    ):
        # Pre-create a scheduled workspace so it has a queue.json entry,
        # then deliberately strip its thread_id to simulate a lost topic.
        mock_platform.create_channel = AsyncMock(return_value=300)
        await topics.create_workspace(
            name="Detached", task_type="scheduled", frequency="daily",
            model="balanced", scheduled_at="08:00", instructions="x",
            manager=agent_manager, work_dir=str(tmp_path),
            platform=mock_platform,
        )
        agent = agent_manager.get("detached")
        agent.thread_id = None
        agent_manager._rebuild_topic_map()
        agent_manager.save_state()

        # The queue.json entry should also reflect the detached state.
        topics._update_queue_entry_thread_id("detached", None)
        queue = json.loads((tmp_path / "data" / "queue.json").read_text())
        detached_entry = next(e for e in queue if e["name"] == "detached")
        assert detached_entry["thread_id"] == ""

        # Pretend the platform hands us a fresh topic id.
        mock_platform.create_channel = AsyncMock(return_value=400)
        mock_platform.send_to_channel = AsyncMock(return_value=True)

        repaired = await topics.heal_detached_workspaces(
            agent_manager, platform=mock_platform,
        )

        assert len(repaired) == 1
        assert repaired[0]["name"] == "detached"
        assert repaired[0]["thread_id"] == 400

        # Agent re-attached to the new topic, and the queue.json entry reflects it.
        assert agent_manager.get("detached").thread_id == 400
        queue = json.loads((tmp_path / "data" / "queue.json").read_text())
        detached_entry = next(e for e in queue if e["name"] == "detached")
        assert detached_entry["thread_id"] == "400"
        # The user receives a welcome message in the freshly attached topic.
        mock_platform.send_to_channel.assert_awaited_once()
        assert mock_platform.send_to_channel.await_args.args[0] == 400

    @pytest.mark.asyncio
    async def test_attached_workspaces_are_left_alone(
        self, tmp_path, agent_manager, mock_platform
    ):
        mock_platform.create_channel = AsyncMock(return_value=500)
        await topics.create_workspace(
            name="Healthy", task_type="interactive", frequency="none",
            model="balanced", scheduled_at="none", instructions="x",
            manager=agent_manager, work_dir=str(tmp_path),
            platform=mock_platform,
        )
        # Reset call count after the create flow above.
        mock_platform.create_channel = AsyncMock(return_value=999)

        repaired = await topics.heal_detached_workspaces(
            agent_manager, platform=mock_platform,
        )
        assert repaired == []
        mock_platform.create_channel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_platform_failure_is_recorded_and_not_fatal(
        self, tmp_path, agent_manager, mock_platform
    ):
        mock_platform.create_channel = AsyncMock(return_value=600)
        await topics.create_workspace(
            name="StillBroken", task_type="interactive", frequency="none",
            model="fast", scheduled_at="none", instructions="x",
            manager=agent_manager, work_dir=str(tmp_path),
            platform=mock_platform,
        )
        agent_manager.get("stillbroken").thread_id = None
        agent_manager.save_state()

        # The next attempt to create a topic fails.
        mock_platform.create_channel = AsyncMock(return_value=None)

        repaired = await topics.heal_detached_workspaces(
            agent_manager, platform=mock_platform,
        )
        assert repaired == []
        # The agent is still detached but the call did not raise.
        assert agent_manager.get("stillbroken").thread_id is None

    @pytest.mark.asyncio
    async def test_no_platform_returns_empty_list(self, agent_manager):
        result = await topics.heal_detached_workspaces(agent_manager, platform=None)
        assert result == []


# ---------------------------------------------------------------------------
# create_continuous_workspace — spec 005 (no sub-topic, parent-chat delivery)
# ---------------------------------------------------------------------------


class TestCreateContinuousWorkspaceSpec005:
    """US1 acceptance: continuous tasks do NOT open a sub-topic; delivery
    target is the parent workspace chat's thread; plan.md is persisted.
    """

    @pytest.fixture
    def program(self):
        return {
            "objective": "Improve docs coverage for the handlers module.",
            "success_criteria": ["All public helpers documented", "No TODOs remain"],
            "constraints": ["Do not rewrite existing docstrings"],
            "checkpoint_policy": "on-demand",
            "context": "See ROB-123 for background.",
            "first_step": {
                "number": 1,
                "description": "List undocumented public helpers.",
            },
        }

    @pytest.mark.asyncio
    async def test_does_not_create_subtopic(
        self, tmp_path, agent_manager, mock_platform, program, monkeypatch,
    ):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous")
        work_dir = tmp_path / "project"
        work_dir.mkdir()

        result = await topics.create_continuous_workspace(
            name="Docs Hunt",
            program=program,
            work_dir=str(work_dir),
            parent_workspace="ops",
            model="powerful",
            manager=agent_manager,
            platform=mock_platform,
            parent_thread_id=42,
        )

        assert result is not None
        # Core spec 005 assertion: no new sub-topic opened.
        mock_platform.create_channel.assert_not_awaited()
        # Delivery target is the parent thread.
        assert result["thread_id"] == 42

    @pytest.mark.asyncio
    async def test_persists_plan_md(
        self, tmp_path, agent_manager, mock_platform, program, monkeypatch,
    ):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous")
        work_dir = tmp_path / "project"
        work_dir.mkdir()

        result = await topics.create_continuous_workspace(
            name="Docs Hunt",
            program=program,
            work_dir=str(work_dir),
            parent_workspace="ops",
            model="powerful",
            manager=agent_manager,
            platform=mock_platform,
            parent_thread_id=42,
        )

        assert result is not None
        plan_path = Path(result["plan_path"])
        assert plan_path.exists()
        content = plan_path.read_text(encoding="utf-8")
        assert "# Plan: Docs Hunt" in content
        assert program["objective"] in content
        assert "All public helpers documented" in content
        assert "Do not rewrite existing docstrings" in content

    @pytest.mark.asyncio
    async def test_state_thread_id_is_parent_thread(
        self, tmp_path, agent_manager, mock_platform, program, monkeypatch,
    ):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous")
        work_dir = tmp_path / "project"
        work_dir.mkdir()

        await topics.create_continuous_workspace(
            name="Docs Hunt",
            program=program,
            work_dir=str(work_dir),
            parent_workspace="ops",
            model="powerful",
            manager=agent_manager,
            platform=mock_platform,
            parent_thread_id=42,
        )

        state_file = tmp_path / "data" / "continuous" / "docs-hunt" / "state.json"
        state = json.loads(state_file.read_text())
        assert state["workspace_thread_id"] == 42
        assert state["plan_path"].endswith("data/continuous/docs-hunt/plan.md")

    @pytest.mark.asyncio
    async def test_queue_entry_uses_parent_thread(
        self, tmp_path, agent_manager, mock_platform, program, monkeypatch,
    ):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous")
        work_dir = tmp_path / "project"
        work_dir.mkdir()

        await topics.create_continuous_workspace(
            name="Docs Hunt",
            program=program,
            work_dir=str(work_dir),
            parent_workspace="ops",
            model="powerful",
            manager=agent_manager,
            platform=mock_platform,
            parent_thread_id=42,
        )

        queue_file = tmp_path / "data" / "queue.json"
        queue = json.loads(queue_file.read_text())
        entries = queue.get("entries") if isinstance(queue, dict) else queue
        continuous_entries = [e for e in entries if e.get("type") == "continuous"]
        assert len(continuous_entries) == 1
        assert continuous_entries[0]["thread_id"] == "42"
        assert continuous_entries[0]["name"] == "docs-hunt"

    @pytest.mark.asyncio
    async def test_agent_registered_with_no_thread_id(
        self, tmp_path, agent_manager, mock_platform, program, monkeypatch,
    ):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous")
        work_dir = tmp_path / "project"
        work_dir.mkdir()

        await topics.create_continuous_workspace(
            name="Docs Hunt",
            program=program,
            work_dir=str(work_dir),
            parent_workspace="ops",
            model="powerful",
            manager=agent_manager,
            platform=mock_platform,
            parent_thread_id=42,
        )

        # Agent must NOT claim the parent workspace's thread_id in the
        # routing map — the parent agent owns thread 42.
        agent = agent_manager.get("docs-hunt")
        assert agent is not None
        assert agent.thread_id is None

    @pytest.mark.asyncio
    async def test_missing_parent_thread_id_returns_none(
        self, tmp_path, agent_manager, mock_platform, program, monkeypatch,
    ):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous")
        work_dir = tmp_path / "project"
        work_dir.mkdir()

        result = await topics.create_continuous_workspace(
            name="Docs Hunt",
            program=program,
            work_dir=str(work_dir),
            parent_workspace="ops",
            model="powerful",
            manager=agent_manager,
            platform=mock_platform,
            parent_thread_id=None,
        )
        assert result is None
        mock_platform.create_channel.assert_not_awaited()
