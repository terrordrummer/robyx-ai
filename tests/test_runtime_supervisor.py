"""RR-07 regression tests for task/process supervision."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

import ai_invoke
import orphan_tracker
import runtime_supervisor as supervisor_module
import scheduler
from execution_policy import UnsupportedExecutionProfile
from runtime_supervisor import RuntimeSupervisor, get_runtime_supervisor
from scheduled_delivery import start_task_delivery_watch


class _ProbeBackend:
    cli_path = "/test/probe-cli"
    name = "Probe CLI"

    def participant_probe_commands(self):
        return [[self.cli_path, "--help"]]

    def validate_participant_probe(self, outputs):
        return None


class _ProbeProcess:
    def __init__(self, pid: int, *, block: bool = False):
        self.pid = pid
        self.returncode = None
        self._block = block
        self.started = asyncio.Event()
        self.exited = asyncio.Event()

    async def communicate(self):
        self.started.set()
        if self._block:
            await asyncio.Event().wait()
        self.returncode = 0
        self.exited.set()
        return b"safe help", b""

    def terminate(self):
        self.returncode = -signal.SIGTERM
        self.exited.set()

    def kill(self):
        self.returncode = -signal.SIGKILL
        self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode


@pytest.mark.asyncio
async def test_participant_probe_is_process_supervised_and_isolated(monkeypatch):
    supervisor = RuntimeSupervisor()
    proc = _ProbeProcess(31001)
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(ai_invoke, "get_runtime_supervisor", lambda: supervisor)
    monkeypatch.setattr(ai_invoke.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    ai_invoke.reset_participant_probe_cache()

    await ai_invoke._ensure_participant_backend_supported(
        _ProbeBackend(), env_overrides={"PROBE_CHILD_ONLY": "yes"},
    )

    kwargs = spawn.await_args.kwargs
    assert kwargs["start_new_session"] is (sys.platform != "win32")
    assert kwargs["env"]["PROBE_CHILD_ONLY"] == "yes"
    assert supervisor.process_count == 0
    orphan_tracker.register.assert_called_once_with(
        proc.pid, owner="participant-probe:Probe CLI",
    )
    orphan_tracker.unregister.assert_called_once_with(proc.pid)


@pytest.mark.asyncio
async def test_participant_probe_cancellation_terminates_reaps_and_does_not_cache(
    monkeypatch,
):
    supervisor = RuntimeSupervisor()
    proc = _ProbeProcess(31002, block=True)
    backend = _ProbeBackend()
    monkeypatch.setattr(ai_invoke, "get_runtime_supervisor", lambda: supervisor)
    monkeypatch.setattr(
        ai_invoke.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    monkeypatch.setattr(
        supervisor,
        "_signal_process",
        lambda tracked, _sig: tracked.proc.terminate(),
    )
    ai_invoke.reset_participant_probe_cache()

    probe = asyncio.create_task(
        ai_invoke._ensure_participant_backend_supported(
            backend, env_overrides={},
        ),
    )
    await proc.started.wait()
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe

    assert proc.returncode == -signal.SIGTERM
    assert supervisor.process_count == 0
    assert (type(backend), backend.cli_path) not in ai_invoke._PARTICIPANT_PROBE_CACHE
    orphan_tracker.unregister.assert_called_with(proc.pid)


@pytest.mark.asyncio
async def test_participant_probe_rejects_success_while_descendant_tree_survives(
    monkeypatch,
):
    supervisor = RuntimeSupervisor()
    proc = _ProbeProcess(31004)
    backend = _ProbeBackend()
    monkeypatch.setattr(ai_invoke, "get_runtime_supervisor", lambda: supervisor)
    monkeypatch.setattr(
        ai_invoke.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    monkeypatch.setattr(supervisor, "process_tree_alive", MagicMock(return_value=True))
    terminate = AsyncMock(return_value=False)
    monkeypatch.setattr(supervisor, "terminate_process", terminate)
    ai_invoke.reset_participant_probe_cache()

    with pytest.raises(UnsupportedExecutionProfile, match="live process tree"):
        await ai_invoke._ensure_participant_backend_supported(
            backend,
            env_overrides={},
        )

    terminate.assert_awaited_once_with(proc, grace_seconds=1.0)
    assert supervisor.process_count == 1
    assert ai_invoke._PARTICIPANT_PROBE_CACHE == {}


@pytest.mark.asyncio
async def test_stop_processes_retrieves_cancelled_waiters(monkeypatch):
    supervisor = RuntimeSupervisor()
    waiter_cancelled = asyncio.Event()

    class StubbornProcess:
        pid = 31003
        returncode = None

        async def wait(self):
            try:
                await asyncio.Event().wait()
            finally:
                waiter_cancelled.set()

    proc = StubbornProcess()
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    monkeypatch.setattr(supervisor, "_signal_process", MagicMock())
    supervisor.track_process(proc, owner="stubborn", process_group=False)

    calls = 0

    async def all_pending(awaitables, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0)
        return set(), set(awaitables)

    monkeypatch.setattr(supervisor_module.asyncio, "wait", all_pending)
    await supervisor._stop_processes(grace_seconds=0.0)

    assert calls == 2
    assert waiter_cancelled.is_set()
    proc.returncode = -signal.SIGKILL
    supervisor.untrack_process(proc)


@pytest.mark.asyncio
async def test_scheduler_spawn_uses_isolated_group_and_child_env(
    tmp_path,
    monkeypatch,
):
    fake_proc = MagicMock()
    fake_proc.pid = 9876
    fake_proc.stdin = None
    create = AsyncMock(return_value=fake_proc)
    track = MagicMock()
    fake_supervisor = MagicMock()
    fake_supervisor.track_process = track
    fake_supervisor.reject_if_closing = MagicMock()
    monkeypatch.setattr(scheduler.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        supervisor_module,
        "get_runtime_supervisor",
        lambda: fake_supervisor,
    )

    result = await scheduler._spawn_ai_subprocess(
        cmd=["agent", "run"],
        stdin_payload=None,
        output_log=tmp_path / "output.log",
        work_dir=str(tmp_path),
        env_overrides={"RR07_CHILD_ONLY": "yes"},
        owner="scheduled:env-test",
    )

    assert result is fake_proc
    kwargs = create.await_args.kwargs
    assert kwargs["start_new_session"] is (sys.platform != "win32")
    assert kwargs["env"]["RR07_CHILD_ONLY"] == "yes"
    if "PATH" in os.environ:
        assert kwargs["env"]["PATH"] == os.environ["PATH"]
    track.assert_called_once_with(
        fake_proc,
        owner="scheduled:env-test",
        process_group=sys.platform != "win32",
    )


@pytest.mark.asyncio
async def test_named_task_is_idempotent_and_exception_is_reported(caplog):
    supervisor = RuntimeSupervisor()
    gate = asyncio.Event()
    calls = 0

    async def background():
        nonlocal calls
        calls += 1
        await gate.wait()
        raise RuntimeError("maintenance exploded")

    first = supervisor.start_once("maintenance", background)
    second = supervisor.start_once("maintenance", background)
    assert first is second
    assert supervisor.task_count == 1

    with caplog.at_level(logging.ERROR, logger="robyx.supervisor"):
        gate.set()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0)

    assert calls == 1
    assert supervisor.task_count == 0
    assert "maintenance exploded" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_escalates_child_then_cancels_tasks(monkeypatch):
    supervisor = RuntimeSupervisor()
    exited = asyncio.Event()
    background_cancelled = asyncio.Event()

    class FakeProcess:
        pid = 43210
        returncode = None
        terminate = MagicMock()
        kill = MagicMock()

        async def wait(self):
            await exited.wait()
            self.returncode = -9
            return self.returncode

    proc = FakeProcess()
    signals = []

    def fake_signal(tracked, sig):
        signals.append(sig)
        if sig == signal.SIGKILL:
            exited.set()

    async def background():
        try:
            await asyncio.Event().wait()
        finally:
            background_cancelled.set()

    monkeypatch.setattr(supervisor, "_signal_process", fake_signal)
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    supervisor.track_process(proc, owner="test", process_group=True)
    supervisor.spawn(background(), name="background")

    await supervisor.shutdown(
        process_grace_seconds=0.01,
        task_grace_seconds=0.2,
    )

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert background_cancelled.is_set()
    assert supervisor.task_count == 0
    assert supervisor.process_count == 0


@pytest.mark.asyncio
async def test_shutdown_rejects_new_tasks_and_closes_coroutine():
    supervisor = RuntimeSupervisor()
    await supervisor.shutdown()

    async def never_started():
        await asyncio.sleep(0)

    coroutine = never_started()
    with pytest.raises(RuntimeError, match="shutting down"):
        supervisor.spawn(coroutine, name="late")
    assert coroutine.cr_frame is None


@pytest.mark.asyncio
async def test_spawn_duplicate_key_closes_unused_coroutine():
    supervisor = RuntimeSupervisor()
    gate = asyncio.Event()

    async def waiting():
        await gate.wait()

    first = supervisor.spawn(waiting(), name="first", key="singleton")
    unused = waiting()
    second = supervisor.spawn(unused, name="second", key="singleton")

    assert second is first
    assert unused.cr_frame is None
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_registry_error_signals_and_fails_closed(monkeypatch, caplog):
    supervisor = RuntimeSupervisor()
    process = _ProbeProcess(pid=50001)
    monkeypatch.setattr(
        orphan_tracker,
        "register",
        MagicMock(side_effect=OSError("registry unavailable")),
    )
    monkeypatch.setattr(
        orphan_tracker,
        "unregister",
        MagicMock(side_effect=OSError("registry unavailable")),
    )

    with pytest.raises(RuntimeError, match="registry unavailable"):
        supervisor.track_process(process, owner="test", process_group=False)
    assert process.returncode == -signal.SIGTERM
    assert await supervisor.terminate_process(process, grace_seconds=0.1)

    assert supervisor.process_count == 0
    assert "Could not persist child PID" in caplog.text


@pytest.mark.asyncio
async def test_terminate_unregistered_process_reaps_after_sigterm(monkeypatch):
    supervisor = RuntimeSupervisor()
    exited = asyncio.Event()

    class Process:
        pid = 50002
        returncode = None

        def terminate(self):
            self.returncode = -signal.SIGTERM
            exited.set()

        def kill(self):
            raise AssertionError("graceful termination must not escalate")

        async def wait(self):
            await exited.wait()
            return self.returncode

    process = Process()
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())

    await supervisor.terminate_process(process, grace_seconds=0.1)

    assert process.returncode == -signal.SIGTERM
    orphan_tracker.unregister.assert_called_once_with(process.pid)


@pytest.mark.asyncio
async def test_repeated_shutdown_reuses_completed_future():
    supervisor = RuntimeSupervisor()

    await supervisor.shutdown()
    await supervisor.shutdown()

    assert supervisor.closing is True
    with pytest.raises(RuntimeError, match="shutting down"):
        supervisor.reject_if_closing()


def test_reset_for_tests_rejects_live_state_and_resets_closed_supervisor():
    supervisor = RuntimeSupervisor()
    supervisor._processes[50003] = MagicMock()
    with pytest.raises(RuntimeError, match="live runtime supervisor"):
        supervisor.reset_for_tests()

    supervisor._processes.clear()
    supervisor._closing = True
    supervisor._shutdown_future = MagicMock()
    supervisor.reset_for_tests()

    assert supervisor.closing is False
    assert supervisor._shutdown_future is None


def test_closed_supervisor_rejects_start_once_and_sync_shutdown_is_empty():
    supervisor = RuntimeSupervisor()
    supervisor._closing = True

    with pytest.raises(RuntimeError, match="shutting down"):
        supervisor.start_once("late", lambda: None)

    supervisor.terminate_processes_sync(grace_seconds=0.0)


def test_signal_refuses_shared_process_group_and_falls_back(monkeypatch, caplog):
    supervisor = RuntimeSupervisor()
    process = MagicMock(pid=50004)
    tracked = supervisor_module._TrackedProcess(
        process,
        owner="misconfigured",
        process_group=True,
    )
    monkeypatch.setattr(supervisor_module.sys, "platform", "linux")
    monkeypatch.setattr(supervisor_module.os, "getpgid", lambda _pid: 49999)
    kill_group = MagicMock()
    monkeypatch.setattr(supervisor_module.os, "killpg", kill_group)

    supervisor._signal_process(tracked, signal.SIGTERM)
    supervisor._signal_process(tracked, signal.SIGKILL)

    kill_group.assert_not_called()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert "not isolated" in caplog.text


@pytest.mark.asyncio
async def test_terminate_process_escalates_after_grace_timeout(monkeypatch):
    supervisor = RuntimeSupervisor()
    process = MagicMock(pid=50005, returncode=None)
    waits = 0

    async def wait():
        nonlocal waits
        waits += 1
        if waits == 1:
            await asyncio.Event().wait()
        process.returncode = -signal.SIGKILL
        return process.returncode

    process.wait = wait
    signals = []
    monkeypatch.setattr(
        supervisor,
        "_signal_process",
        lambda _tracked, sig: signals.append(sig),
    )
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())

    await supervisor.terminate_process(process, grace_seconds=0.001)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    orphan_tracker.unregister.assert_called_once_with(process.pid)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
async def test_shutdown_terminates_and_reaps_real_orphan_candidate(
    monkeypatch,
):
    supervisor = RuntimeSupervisor()
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        start_new_session=True,
    )
    supervisor.track_process(proc, owner="real-child", process_group=True)

    await supervisor.shutdown(
        process_grace_seconds=0.2,
        task_grace_seconds=0.2,
    )

    assert proc.returncode is not None
    assert supervisor.process_count == 0


@pytest.mark.asyncio
async def test_delivery_watcher_reaps_without_platform_and_cleans_registry(
    tmp_path,
    monkeypatch,
):
    supervisor = get_runtime_supervisor()
    if supervisor.closing:
        supervisor.reset_for_tests()
    assert supervisor.task_count == 0
    assert supervisor.process_count == 0
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())

    lock_file = tmp_path / "lock"
    lock_file.write_text("123\n")
    output_log = tmp_path / "output.log"
    output_log.write_text("done")
    proc = MagicMock(pid=123)
    proc.pid = 123
    proc.returncode = 0
    proc.wait = AsyncMock(return_value=0)

    watcher = start_task_delivery_watch(
        {"name": "headless"},
        proc,
        output_log,
        lock_file,
        None,
        MagicMock(),
        MagicMock(),
    )
    await watcher

    assert not lock_file.exists()
    assert supervisor.task_count == 0
    assert supervisor.process_count == 0


@pytest.mark.asyncio
async def test_delivery_watcher_logs_delivery_exception_and_still_cleans(
    tmp_path,
    monkeypatch,
    caplog,
):
    import scheduled_delivery as delivery

    supervisor = get_runtime_supervisor()
    if supervisor.closing:
        supervisor.reset_for_tests()
    assert supervisor.task_count == 0
    assert supervisor.process_count == 0

    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    monkeypatch.setattr(
        delivery,
        "deliver_task_output",
        AsyncMock(side_effect=RuntimeError("relay failed")),
    )

    lock_file = tmp_path / "lock"
    lock_file.write_text("456\n")
    output_log = tmp_path / "output.log"
    output_log.write_text("done")
    proc = MagicMock(pid=456)
    proc.pid = 456
    proc.returncode = 0
    proc.wait = AsyncMock(return_value=0)
    logger = logging.getLogger("test.delivery.supervision")

    with caplog.at_level(logging.ERROR, logger=logger.name):
        watcher = start_task_delivery_watch(
            {"name": "broken-relay"},
            proc,
            output_log,
            lock_file,
            AsyncMock(),
            MagicMock(),
            logger,
        )
        await watcher

    assert "relay failed" in caplog.text
    assert not lock_file.exists()
    assert supervisor.task_count == 0
    assert supervisor.process_count == 0


@pytest.mark.asyncio
async def test_delivery_watcher_refuses_success_while_descendant_survives(
    tmp_path,
    monkeypatch,
):
    import scheduled_delivery as delivery

    supervisor = get_runtime_supervisor()
    if supervisor.closing:
        supervisor.reset_for_tests()
    assert supervisor.process_count == 0
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    relay = AsyncMock(return_value=True)
    monkeypatch.setattr(delivery, "deliver_task_output", relay)
    proc = MagicMock(pid=45601, returncode=0)
    proc.wait = AsyncMock(return_value=0)
    lock_file = tmp_path / "lock"
    lock_file.write_text("45601\n")
    output_log = tmp_path / "output.log"
    output_log.write_text("done")
    logger = logging.getLogger("test.delivery.descendant")
    monkeypatch.setattr(supervisor, "process_tree_alive", MagicMock(return_value=True))
    terminate = AsyncMock(return_value=False)
    monkeypatch.setattr(supervisor, "terminate_process", terminate)

    watcher = start_task_delivery_watch(
        {"name": "descendant-survives"},
        proc,
        output_log,
        lock_file,
        AsyncMock(),
        MagicMock(),
        logger,
    )
    await watcher

    terminate.assert_awaited_once_with(proc, grace_seconds=2.0)
    relay.assert_not_awaited()
    assert lock_file.exists()
    supervisor._processes.pop(proc.pid, None)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
async def test_terminate_reaps_leader_and_kills_term_ignoring_grandchild(
    tmp_path,
    monkeypatch,
):
    """A leader exiting on TERM must not let its grandchild escape the group."""
    supervisor = RuntimeSupervisor()
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    grandchild_pid_file = tmp_path / "grandchild.pid"
    leader = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import os,signal,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
            "open(sys.argv[1],'w').write(str(child.pid)); "
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(60)"
        ),
        str(grandchild_pid_file),
        start_new_session=True,
    )
    supervisor.track_process(leader, owner="tree", process_group=True)
    for _ in range(100):
        if grandchild_pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert grandchild_pid_file.exists()
    grandchild_pid = int(grandchild_pid_file.read_text())

    assert await supervisor.terminate_process(leader, grace_seconds=0.1) is True
    assert leader.returncode is not None
    assert supervisor.process_count == 0
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


@pytest.mark.asyncio
async def test_concurrent_terminate_calls_coalesce(monkeypatch):
    supervisor = RuntimeSupervisor()
    exited = asyncio.Event()

    class Process:
        pid = 62001
        returncode = None

        async def wait(self):
            await exited.wait()
            self.returncode = -signal.SIGTERM
            return self.returncode

        def terminate(self):
            exited.set()

        def kill(self):
            raise AssertionError("must not escalate")

    process = Process()
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    supervisor.track_process(process, owner="race", process_group=False)
    signal_spy = MagicMock(side_effect=supervisor._signal_process)
    monkeypatch.setattr(supervisor, "_signal_process", signal_spy)

    first, second = await asyncio.gather(
        supervisor.terminate_process(process, grace_seconds=0.2),
        supervisor.terminate_process_by_pid(process.pid, grace_seconds=0.2),
    )

    assert first is True and second is True
    assert signal_spy.call_count == 1
    assert supervisor.process_count == 0


@pytest.mark.asyncio
async def test_cancelled_terminate_and_concurrent_shutdown_still_reap_tree(monkeypatch):
    supervisor = RuntimeSupervisor()
    exited = asyncio.Event()

    class SlowProcess:
        pid = 62011
        returncode = None

        async def wait(self):
            await exited.wait()
            return self.returncode

        def terminate(self):
            asyncio.get_running_loop().call_later(0.02, self._exit)

        def _exit(self):
            self.returncode = -signal.SIGTERM
            exited.set()

        def kill(self):
            self.returncode = -signal.SIGKILL
            exited.set()

    process = SlowProcess()
    unregister = MagicMock()
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", unregister)
    supervisor.track_process(process, owner="race", process_group=False)

    termination = asyncio.create_task(
        supervisor.terminate_process(process, grace_seconds=0.2),
    )
    await asyncio.sleep(0)
    termination.cancel()
    shutdown = asyncio.create_task(
        supervisor.shutdown(process_grace_seconds=0.2),
    )
    with pytest.raises(asyncio.CancelledError):
        await termination
    await shutdown

    assert process.returncode == -signal.SIGTERM
    assert supervisor.process_count == 0
    unregister.assert_called_with(process.pid)


@pytest.mark.asyncio
async def test_shutdown_waits_for_maintenance_writer_before_closing(monkeypatch):
    import maintenance

    supervisor = RuntimeSupervisor()
    observed = []
    gate = MagicMock()

    async def cancel_writer(*, timeout):
        observed.append(("writer", supervisor.closing, timeout))
        await asyncio.sleep(0)
        return True

    gate.cancel_active_writer = cancel_writer
    monkeypatch.setattr(maintenance, "get_maintenance_gate", lambda: gate)

    await supervisor.shutdown(maintenance_writer_grace_seconds=7.0)

    assert observed == [("writer", False, 7.0)]
    assert supervisor.closing is True


@pytest.mark.asyncio
async def test_drain_processes_keeps_supervisor_open(monkeypatch):
    supervisor = RuntimeSupervisor()
    process = MagicMock(pid=62002, returncode=0)
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    supervisor.track_process(process, owner="update", process_group=False)

    assert await supervisor.drain_processes(grace_seconds=0.0) is True
    assert supervisor.closing is False
    assert supervisor.process_count == 0
    assert await supervisor.drain_processes() is True


@pytest.mark.asyncio
async def test_terminate_by_pid_platform_and_legacy_branches(monkeypatch):
    supervisor = RuntimeSupervisor()
    alive_values = [True]
    monkeypatch.setattr(supervisor_module.sys, "platform", "win32")
    monkeypatch.setattr(
        "process.is_pid_alive",
        lambda _pid: alive_values.pop() if alive_values else False,
    )
    taskkill = AsyncMock(return_value=True)
    monkeypatch.setattr(supervisor, "_taskkill_tree", taskkill)
    assert await supervisor.terminate_process_by_pid(62003, grace_seconds=0.1)
    taskkill.assert_awaited_once_with(62003, force=False)

    monkeypatch.setattr(supervisor_module.sys, "platform", "linux")
    monkeypatch.setattr("process.is_pid_alive", lambda _pid: False)
    assert await supervisor.terminate_process_by_pid(62004)

    monkeypatch.setattr("process.is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        supervisor_module.os,
        "getpgid",
        MagicMock(side_effect=OSError("denied")),
    )
    assert await supervisor.terminate_process_by_pid(62005) is False


@pytest.mark.asyncio
async def test_windows_taskkill_command_targets_complete_tree(monkeypatch):
    child = MagicMock(pid=70001, returncode=0)
    child.wait = AsyncMock(return_value=0)
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr(supervisor_module.asyncio, "create_subprocess_exec", spawn)

    assert await RuntimeSupervisor._taskkill_tree(62009, force=True)
    assert spawn.await_args.args == ("taskkill", "/PID", "62009", "/T", "/F")


@pytest.mark.asyncio
async def test_windows_taskkill_cancellation_kills_and_reaps_helper(monkeypatch):
    exited = asyncio.Event()

    class Child:
        pid = 70002
        returncode = None

        async def wait(self):
            await exited.wait()
            self.returncode = -signal.SIGKILL
            return self.returncode

        def kill(self):
            exited.set()

    child = Child()
    monkeypatch.setattr(
        supervisor_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=child),
    )
    operation = asyncio.create_task(
        RuntimeSupervisor._taskkill_tree(62010, force=False),
    )
    await asyncio.sleep(0)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert exited.is_set()
    assert child.returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_windows_taskkill_timeout_and_spawn_failure_are_bounded(monkeypatch):
    child = MagicMock(pid=70003, returncode=None)
    child.wait = AsyncMock(return_value=0)
    child.kill = MagicMock()
    monkeypatch.setattr(
        supervisor_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=child),
    )

    async def immediate_timeout(awaitable, *, timeout):
        del timeout
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        supervisor_module.asyncio,
        "wait_for",
        immediate_timeout,
    )

    assert await RuntimeSupervisor._taskkill_tree(62011, force=True) is False
    child.kill.assert_called_once_with()
    assert child.wait.await_count == 1

    monkeypatch.setattr(
        supervisor_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError("taskkill unavailable")),
    )
    assert await RuntimeSupervisor._taskkill_tree(62012, force=False) is False


def test_windows_taskkill_sync_reports_success_and_failure(monkeypatch):
    run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(subprocess, "run", run)
    assert RuntimeSupervisor._taskkill_tree_sync(62013) is True
    assert run.call_args.args[0] == ["taskkill", "/PID", "62013", "/T", "/F"]

    run.side_effect = subprocess.TimeoutExpired("taskkill", 5)
    assert RuntimeSupervisor._taskkill_tree_sync(62013) is False


@pytest.mark.asyncio
async def test_windows_tracked_termination_escalates_complete_tree(monkeypatch):
    supervisor = RuntimeSupervisor()
    process = MagicMock(pid=62014, returncode=None)
    original_platform = sys.platform
    monkeypatch.setattr(supervisor_module.sys, "platform", "win32")
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    unregister = MagicMock()
    monkeypatch.setattr(orphan_tracker, "unregister", unregister)
    supervisor.track_process(process, owner="windows-tree", process_group=False)
    taskkill = AsyncMock(side_effect=[True, True])
    wait_tree = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(supervisor, "_taskkill_tree", taskkill)
    monkeypatch.setattr(supervisor, "_wait_for_process_tree", wait_tree)

    assert await supervisor.terminate_process(process, grace_seconds=0.25) is True
    assert [call.kwargs["force"] for call in taskkill.await_args_list] == [False, True]
    assert [call.args[1] for call in wait_tree.await_args_list] == [0.25, 2.0]
    unregister.assert_called_once_with(process.pid)
    # ``sys`` is a process-global module; restore before pytest tears down the
    # async loop so selector cleanup does not observe a fake Windows runtime.
    monkeypatch.setattr(supervisor_module.sys, "platform", original_platform)


@pytest.mark.asyncio
async def test_terminate_by_pid_shared_group_escalates_leader(monkeypatch):
    supervisor = RuntimeSupervisor()
    alive = iter([True, True, True, False, False])
    monkeypatch.setattr("process.is_pid_alive", lambda _pid: next(alive))
    monkeypatch.setattr(supervisor_module.os, "getpgid", lambda _pid: 42)
    monkeypatch.setattr(supervisor_module.os, "getpgrp", lambda: 42)
    kill = MagicMock()
    monkeypatch.setattr(supervisor_module.os, "kill", kill)
    monkeypatch.setattr(supervisor_module.asyncio, "sleep", AsyncMock())

    assert await supervisor.terminate_process_by_pid(62006, grace_seconds=0.0)
    assert [item.args for item in kill.call_args_list] == [
        (62006, signal.SIGTERM),
        (62006, signal.SIGKILL),
    ]


@pytest.mark.asyncio
async def test_terminate_by_pid_isolated_group_term_and_kill(monkeypatch):
    supervisor = RuntimeSupervisor()
    monkeypatch.setattr("process.is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(supervisor_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(supervisor_module.os, "getpgrp", lambda: 10)
    group_alive = iter([True, True, False, False])
    monkeypatch.setattr(
        supervisor,
        "_process_group_alive",
        lambda _pgid: next(group_alive),
    )
    killpg = MagicMock()
    monkeypatch.setattr(supervisor_module.os, "killpg", killpg)
    monkeypatch.setattr(supervisor_module.asyncio, "sleep", AsyncMock())

    assert await supervisor.terminate_process_by_pid(62007, grace_seconds=0.0)
    assert [item.args for item in killpg.call_args_list] == [
        (62007, signal.SIGTERM),
        (62007, signal.SIGKILL),
    ]


def test_group_liveness_and_untrack_retention_branches(monkeypatch):
    supervisor = RuntimeSupervisor()
    monkeypatch.setattr(supervisor_module.sys, "platform", "linux")
    monkeypatch.setattr(
        supervisor_module.os,
        "killpg",
        MagicMock(side_effect=PermissionError),
    )
    assert supervisor._process_group_alive(42) is True
    monkeypatch.setattr(
        supervisor_module.os,
        "killpg",
        MagicMock(side_effect=OSError),
    )
    assert supervisor._process_group_alive(42) is False

    process = MagicMock(pid=62008, returncode=None)
    supervisor._processes[process.pid] = supervisor_module._TrackedProcess(
        process,
        "live",
        False,
    )
    assert supervisor.process_tree_alive(process.pid) is True
    assert supervisor.process_tree_alive(99999) is False
    assert supervisor.untrack_process(process) is False
    process.returncode = 0
    assert supervisor.untrack_process(process) is True


def test_group_signal_and_track_while_closing_branches(monkeypatch):
    supervisor = RuntimeSupervisor()
    monkeypatch.setattr(supervisor_module.sys, "platform", "linux")
    killpg = MagicMock()
    monkeypatch.setattr(supervisor_module.os, "killpg", killpg)
    assert supervisor._process_group_alive(77) is True

    tracked = supervisor_module._TrackedProcess(
        MagicMock(pid=77, returncode=None),
        "gone",
        True,
        77,
    )
    killpg.side_effect = ProcessLookupError
    supervisor._signal_process(tracked, signal.SIGTERM)

    done = MagicMock()
    done.done.return_value = True
    supervisor._named_tasks["done"] = done
    assert supervisor.get_named_task("done") is None

    supervisor._closing = True
    process = MagicMock(pid=62009, returncode=None)
    monkeypatch.setattr(supervisor_module.os, "getpgid", lambda _pid: 42)
    monkeypatch.setattr(supervisor_module.os, "getpgrp", lambda: 42)
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    signal_process = MagicMock()
    monkeypatch.setattr(supervisor, "_signal_process", signal_process)
    with pytest.raises(RuntimeError, match="shutting down"):
        supervisor.track_process(process, owner="late", process_group=True)
    signal_process.assert_called_once()
    process.returncode = 0
    supervisor.untrack_process(process, force=True)
