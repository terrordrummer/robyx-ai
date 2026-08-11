"""Same-name continuous creation reservation and compensating rollback."""

from __future__ import annotations

import asyncio
import json
import subprocess
from unittest.mock import AsyncMock

import pytest
from task_scope import TaskScope


_SCOPE = TaskScope("telegram", "-1001", 42)


@pytest.mark.asyncio
async def test_same_name_creation_is_reserved_across_awaits(
    tmp_path,
    monkeypatch,
    agent_manager,
):
    import continuous
    import scheduler
    import topics

    monkeypatch.setattr(continuous, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(scheduler, "QUEUE_FILE", tmp_path / "queue.json")
    (tmp_path / "queue.json").write_text("[]")
    entered = 0

    async def fake_reserved(**kwargs):
        nonlocal entered
        safe_name = topics._sanitize_task_name(kwargs["name"])
        topics._validate_new_agent_name(
            safe_name,
            kwargs["manager"],
            "continuous workspace",
        )
        entered += 1
        await asyncio.sleep(0.02)
        kwargs["manager"].add_agent(
            name=safe_name,
            work_dir=str(tmp_path),
            description="continuous",
            agent_type="workspace",
        )
        return {"name": safe_name}

    monkeypatch.setattr(topics, "_create_continuous_workspace_reserved", fake_reserved)
    calls = [
        topics.create_continuous_workspace(
            name="Same Name",
            program={"objective": "x"},
            work_dir=str(tmp_path),
            parent_workspace="ops",
            model="m",
            manager=agent_manager,
            parent_thread_id=42,
            workspace_scope=_SCOPE,
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(*calls, return_exceptions=True)

    assert sum(isinstance(value, dict) for value in results) == 1
    errors = [value for value in results if isinstance(value, Exception)]
    assert len(errors) == 1
    assert "already in use" in str(errors[0])
    assert entered == 1


@pytest.mark.asyncio
async def test_failed_creation_rolls_back_files_queue_agent_and_topic(
    tmp_path,
    monkeypatch,
    agent_manager,
):
    import continuous
    import scheduler
    import topics

    continuous_dir = tmp_path / "continuous"
    queue_file = tmp_path / "queue.json"
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(continuous, "CONTINUOUS_DIR", continuous_dir)
    monkeypatch.setattr(scheduler, "QUEUE_FILE", queue_file)
    monkeypatch.setattr(topics, "AGENTS_DIR", agents_dir)
    unrelated = {
        "id": "unrelated",
        "type": "reminder",
        "status": "pending",
        "message": "keep me",
        "fire_at": "2099-01-01T00:00:00+00:00",
    }
    queue_file.write_text(json.dumps([unrelated]))
    platform = AsyncMock()
    platform.archive_topic = AsyncMock(return_value=True)

    async def partial_then_fail(**kwargs):
        name = "rollback-me"
        kwargs["_reservation"].dedicated_thread_id = 777
        state_path = continuous.state_file_path(name)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text('{"partial":true}')
        (state_path.parent / "plan.md").write_text("partial")
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / (name + ".md")).write_text("partial")
        scheduler.add_task({
            "id": "partial",
            "type": "continuous",
            "name": name,
            "thread_id": "777",
            "state_file": str(state_path),
        }, scope=kwargs["workspace_scope"])
        kwargs["manager"].add_agent(
            name=name,
            work_dir=str(tmp_path),
            description="partial",
            agent_type="workspace",
        )
        raise RuntimeError("late failure")

    monkeypatch.setattr(topics, "_create_continuous_workspace_reserved", partial_then_fail)
    with pytest.raises(RuntimeError, match="late failure"):
        await topics.create_continuous_workspace(
            name="rollback-me",
            program={"objective": "x"},
            work_dir=str(tmp_path),
            parent_workspace="ops",
            model="m",
            manager=agent_manager,
            platform=platform,
            parent_thread_id=42,
            workspace_scope=_SCOPE,
        )

    assert scheduler.load_queue() == [unrelated]
    assert agent_manager.get("rollback-me") is None
    assert not continuous.state_file_path("rollback-me").exists()
    assert not (agents_dir / "rollback-me.md").exists()
    platform.archive_topic.assert_awaited_once_with(777, "rollback-me")


@pytest.mark.asyncio
async def test_late_failure_restores_existing_git_branch_and_deletes_only_created_branch(
    tmp_path,
    monkeypatch,
    agent_manager,
):
    import continuous
    import scheduler
    import topics

    work_dir = tmp_path / "project"
    work_dir.mkdir()
    subprocess.run(["git", "init", str(work_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work_dir), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(work_dir), "config", "user.name", "Test"],
        check=True,
    )
    (work_dir / "seed.txt").write_text("seed")
    subprocess.run(["git", "-C", str(work_dir), "add", "seed.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(work_dir), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    original_branch = subprocess.run(
        ["git", "-C", str(work_dir), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(continuous, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(scheduler, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(topics, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "queue.json").write_text("[]")
    platform = AsyncMock()
    platform.create_channel.return_value = 888
    platform.edit_topic_title.return_value = True
    platform.archive_topic.return_value = True
    original_add = agent_manager.add_agent

    def add_then_fail(**kwargs):
        original_add(**kwargs)
        raise RuntimeError("late manager failure")

    monkeypatch.setattr(agent_manager, "add_agent", add_then_fail)

    with pytest.raises(RuntimeError, match="late manager failure"):
        await topics.create_continuous_workspace(
            name="Git Rollback",
            program={"objective": "x"},
            work_dir=str(work_dir),
            parent_workspace="ops",
            model="m",
            manager=agent_manager,
            platform=platform,
            parent_thread_id=42,
            workspace_scope=_SCOPE,
        )

    current_branch = subprocess.run(
        ["git", "-C", str(work_dir), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    branches = subprocess.run(
        ["git", "-C", str(work_dir), "branch", "--list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert current_branch == original_branch
    assert "continuous/git-rollback" not in branches
    assert (work_dir / ".git").exists()


@pytest.mark.asyncio
async def test_cancellation_after_topic_creation_rolls_back_all_side_effects(
    tmp_path,
    monkeypatch,
    agent_manager,
):
    import continuous
    import scheduler
    import topics

    work_dir = tmp_path / "project"
    work_dir.mkdir()
    monkeypatch.setattr(continuous, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(scheduler, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(topics, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "queue.json").write_text("[]")
    marker_started = asyncio.Event()
    platform = AsyncMock()
    platform.create_channel.return_value = 889
    platform.archive_topic.return_value = True

    async def block_marker(*_args, **_kwargs):
        marker_started.set()
        await asyncio.Event().wait()

    platform.edit_topic_title.side_effect = block_marker
    creation = asyncio.create_task(topics.create_continuous_workspace(
        name="Cancel Me",
        program={"objective": "x"},
        work_dir=str(work_dir),
        parent_workspace="ops",
        model="m",
        manager=agent_manager,
        platform=platform,
        parent_thread_id=42,
        workspace_scope=_SCOPE,
    ))
    await marker_started.wait()
    creation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creation

    platform.archive_topic.assert_awaited_once_with(889, "Cancel Me")
    assert scheduler.load_queue() == []
    assert agent_manager.get("cancel-me") is None
    assert not continuous.state_file_path("cancel-me").exists()
    assert not (tmp_path / "agents" / "cancel-me.md").exists()
    assert not (work_dir / ".git").exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_git_compensation(
    tmp_path,
    monkeypatch,
    agent_manager,
):
    import topics

    compensation_started = asyncio.Event()
    allow_compensation = asyncio.Event()
    compensated = asyncio.Event()

    async def partial_then_cancel(**kwargs):
        kwargs["_reservation"].git_info = {"created_repo": True}
        kwargs["_reservation"].git_work_dir = str(tmp_path)
        await asyncio.Event().wait()

    async def blocked_compensation(_work_dir, _git_info):
        compensation_started.set()
        await allow_compensation.wait()
        compensated.set()
        return True

    monkeypatch.setattr(topics, "_create_continuous_workspace_reserved", partial_then_cancel)
    monkeypatch.setattr(topics, "_compensate_git_setup", blocked_compensation)
    creation = asyncio.create_task(topics.create_continuous_workspace(
        name="Cancel Compensation",
        program={"objective": "x"},
        work_dir=str(tmp_path),
        parent_workspace="ops",
        model="m",
        manager=agent_manager,
        parent_thread_id=42,
        workspace_scope=_SCOPE,
    ))
    await asyncio.sleep(0)
    creation.cancel()
    await compensation_started.wait()
    creation.cancel()
    allow_compensation.set()
    with pytest.raises(asyncio.CancelledError):
        await creation
    assert compensated.is_set()
