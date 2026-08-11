import asyncio

import pytest

from maintenance import (
    MaintenanceActiveError,
    MaintenanceBusyError,
    MaintenanceGate,
    get_maintenance_gate,
    maintenance_active,
)


@pytest.mark.asyncio
async def test_exclusive_waits_for_reader_and_rejects_new_work():
    gate = MaintenanceGate()
    reader_entered = asyncio.Event()
    release_reader = asyncio.Event()

    async def reader():
        async with gate.shared():
            reader_entered.set()
            await release_reader.wait()

    first = asyncio.create_task(reader())
    await reader_entered.wait()

    quiesce_entered = asyncio.Event()

    async def quiesce():
        quiesce_entered.set()
        release_reader.set()

    async def writer():
        async with gate.exclusive(quiesce=quiesce, wait_timeout=1):
            assert gate.reader_count == 0

            async def rejected_reader():
                with pytest.raises(MaintenanceActiveError):
                    async with gate.shared():
                        pass

            await asyncio.create_task(rejected_reader())

    await asyncio.wait_for(writer(), timeout=2)
    await first
    assert quiesce_entered.is_set()
    assert not gate.active


@pytest.mark.asyncio
async def test_shared_lease_is_task_reentrant():
    gate = MaintenanceGate()
    async with gate.shared():
        async with gate.shared():
            assert gate.reader_count == 1
    assert gate.reader_count == 0


@pytest.mark.asyncio
async def test_exclusive_upgrade_is_refused_instead_of_deadlocking():
    gate = MaintenanceGate()
    async with gate.shared():
        with pytest.raises(MaintenanceBusyError, match="upgrade"):
            async with gate.exclusive():
                pass


@pytest.mark.asyncio
async def test_second_writer_fails_fast():
    gate = MaintenanceGate()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first_writer():
        async with gate.exclusive():
            entered.set()
            await release.wait()

    first = asyncio.create_task(first_writer())
    await entered.wait()
    with pytest.raises(MaintenanceBusyError, match="already"):
        async with gate.exclusive():
            pass
    release.set()
    await first


@pytest.mark.asyncio
async def test_cancelled_handoff_notifies_waiting_writer():
    gate = MaintenanceGate()
    async with gate.shared():
        handoff = gate.handoff_shared()

    entered = asyncio.Event()

    async def writer():
        async with gate.exclusive(wait_timeout=1):
            entered.set()

    task = asyncio.create_task(writer())
    await asyncio.sleep(0)
    assert gate.active
    handoff.cancel()
    handoff.cancel()  # idempotent after a synchronous spawn failure
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    await task


@pytest.mark.asyncio
async def test_handoff_retains_shared_reader_until_spawned_work_finishes():
    gate = MaintenanceGate()
    async with gate.shared():
        handoff = gate.handoff_shared()
    assert gate.reader_count == 1

    async with handoff:
        assert gate.reader_count == 1
    assert gate.reader_count == 0

    with pytest.raises(RuntimeError, match="no longer valid"):
        async with handoff:
            pass


@pytest.mark.asyncio
async def test_handoff_requires_an_existing_shared_lease():
    gate = MaintenanceGate()
    with pytest.raises(RuntimeError, match="existing lease"):
        gate.handoff_shared()


@pytest.mark.asyncio
async def test_exclusive_and_shared_are_reentrant_for_writer_task():
    gate = MaintenanceGate()
    async with gate.exclusive():
        async with gate.exclusive():
            async with gate.shared():
                assert gate.active
    assert not gate.active


@pytest.mark.asyncio
async def test_exclusive_timeout_reopens_gate_for_readers():
    gate = MaintenanceGate()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def reader():
        async with gate.shared():
            entered.set()
            await release.wait()

    active_reader = asyncio.create_task(reader())
    await entered.wait()
    with pytest.raises(MaintenanceBusyError, match="deadline"):
        async with gate.exclusive(wait_timeout=0.001):
            pass
    assert not gate.active
    async with gate.shared():
        pass
    release.set()
    await active_reader


@pytest.mark.asyncio
async def test_event_queued_during_failed_writer_intent_is_not_dropped():
    gate = MaintenanceGate()
    reader_entered = asyncio.Event()
    release_reader = asyncio.Event()
    observed = []

    async def reader():
        async with gate.shared():
            reader_entered.set()
            await release_reader.wait()

    active_reader = asyncio.create_task(reader())
    await reader_entered.wait()

    async def event_callback():
        async with gate.shared():
            observed.append("ran")

    async def writer():
        with pytest.raises(MaintenanceBusyError, match="deadline"):
            async with gate.exclusive(wait_timeout=0.02):
                pass

    update = asyncio.create_task(writer())
    while not gate.active:
        await asyncio.sleep(0)
    event = asyncio.create_task(gate.defer(event_callback))

    await update
    await event
    assert observed == ["ran"]
    release_reader.set()
    await active_reader


@pytest.mark.asyncio
async def test_force_requiesces_child_spawned_after_first_drain_snapshot():
    gate = MaintenanceGate()
    first_quiesce = asyncio.Event()
    child_released = asyncio.Event()
    quiesce_calls = 0

    async def scheduler_reader():
        async with gate.shared():
            await first_quiesce.wait()
            handoff = gate.handoff_shared()

        async with handoff:
            await child_released.wait()

    scheduler = asyncio.create_task(scheduler_reader())
    await asyncio.sleep(0)

    async def drain_process_snapshot():
        nonlocal quiesce_calls
        quiesce_calls += 1
        if quiesce_calls == 1:
            # The scheduler was already a reader but had not spawned its
            # process when the updater inspected the supervisor.
            first_quiesce.set()
        else:
            child_released.set()

    async with gate.exclusive(
        quiesce=drain_process_snapshot,
        wait_timeout=1,
    ):
        assert quiesce_calls >= 2
        assert gate.reader_count == 0
    await scheduler


@pytest.mark.asyncio
async def test_gateway_event_is_deferred_once_onto_final_data_tree():
    gate = MaintenanceGate()
    data = {"phase": "before"}
    observed = []

    async def event_callback():
        async with gate.shared():
            observed.append(data["phase"])

    async with gate.exclusive():
        event = asyncio.create_task(gate.defer(event_callback))
        await asyncio.sleep(0)
        assert observed == []
        data["phase"] = "final"

    await event
    assert observed == ["final"]


@pytest.mark.asyncio
async def test_deferred_event_runs_immediately_when_writer_already_released():
    gate = MaintenanceGate()
    calls = 0

    async def event_callback():
        nonlocal calls
        calls += 1
        return "ok"

    assert await gate.defer(event_callback) == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_deferred_events_drain_before_exclusive_finalize():
    gate = MaintenanceGate()
    order = []

    async def event_callback():
        order.append("event")

    async def finalize():
        order.append("finalize")

    async with gate.exclusive(finalize=finalize):
        event = asyncio.create_task(gate.defer(event_callback))
        await asyncio.sleep(0)

    await event
    assert order == ["event", "finalize"]


@pytest.mark.asyncio
async def test_poisoned_gate_permanently_rejects_runtime_and_updates():
    gate = MaintenanceGate()
    gate.poison("restored disk/live state mismatch")

    assert gate.active
    with pytest.raises(MaintenanceActiveError, match="recovery is incomplete"):
        async with gate.shared():
            pass
    with pytest.raises(MaintenanceBusyError, match="recovery is incomplete"):
        async with gate.exclusive():
            pass
    with pytest.raises(MaintenanceActiveError, match="recovery is incomplete"):
        await gate.defer(lambda: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_event_queued_before_failed_recovery_is_rejected_not_mutated(caplog):
    gate = MaintenanceGate()
    mutations = []

    async def event_callback():
        mutations.append("unsafe")

    async with gate.exclusive():
        event = asyncio.create_task(gate.defer(event_callback))
        await asyncio.sleep(0)
        gate.poison("injected restore failure")

    with pytest.raises(MaintenanceActiveError, match="recovery is incomplete"):
        await event
    assert mutations == []
    assert "deferred gateway event" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_cancels_writer_and_waits_for_rollback_finally():
    gate = MaintenanceGate()
    entered = asyncio.Event()
    rollback_complete = asyncio.Event()

    async def update_task():
        async with gate.exclusive():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                rollback_complete.set()

    update = asyncio.create_task(update_task())
    await entered.wait()
    assert gate.writer_task is update

    assert await gate.cancel_active_writer(timeout=1.0)
    assert update.cancelled()
    assert rollback_complete.is_set()
    assert gate.writer_task is None
    assert not gate.active


@pytest.mark.asyncio
async def test_shutdown_writer_cancel_is_noop_for_current_or_absent_task():
    gate = MaintenanceGate()
    assert await gate.cancel_active_writer(timeout=0.01)
    async with gate.exclusive():
        assert gate.writer_task is asyncio.current_task()
        assert await gate.cancel_active_writer(timeout=0.01)


@pytest.mark.asyncio
async def test_shutdown_writer_cancel_timeout_is_bounded_and_observable():
    gate = MaintenanceGate()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_update():
        async with gate.exclusive():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

    update = asyncio.create_task(stubborn_update())
    await entered.wait()
    assert not await gate.cancel_active_writer(timeout=0.001)
    assert not update.done()
    release.set()
    await update
    assert not gate.active


@pytest.mark.asyncio
async def test_shutdown_writer_failure_after_cancel_is_fully_observed():
    gate = MaintenanceGate()
    entered = asyncio.Event()

    async def failed_rollback():
        async with gate.exclusive():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise RuntimeError("injected rollback failure")

    update = asyncio.create_task(failed_rollback())
    await entered.wait()
    assert await gate.cancel_active_writer(timeout=1)
    assert update.done()
    assert not gate.active


def test_global_gate_accessors_report_idle_state():
    assert get_maintenance_gate() is not None
    assert maintenance_active() is get_maintenance_gate().active
