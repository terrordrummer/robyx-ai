"""Process-wide reader/writer gate for destructive maintenance.

Ordinary runtime work holds a shared lease while it may mutate state.  An
update takes the exclusive lease, which first blocks new work and then waits
for all in-flight leases to drain.  Leases are re-entrant per asyncio task so
message routing can call ``invoke_ai`` and delivery helpers without deadlock.
"""

from __future__ import annotations

import asyncio
import logging
from asyncio import wait_for as _wait_for
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, AsyncIterator, Awaitable, Callable


log = logging.getLogger("robyx.maintenance")


MAINTENANCE_MESSAGE = "Robyx is applying an update; try again after the restart."


class MaintenanceActiveError(RuntimeError):
    """New runtime work was refused while maintenance is pending/active."""


class MaintenanceBusyError(RuntimeError):
    """An exclusive lease could not safely quiesce the runtime."""


class SharedLeaseHandoff:
    """A pre-authorised shared lease transferred to a spawned child task."""

    def __init__(self, gate: "MaintenanceGate") -> None:
        self._gate = gate
        self._consumed = False
        self._cancelled = False

    def cancel(self) -> None:
        """Release a handoff when spawning the target task failed synchronously."""
        if self._consumed or self._cancelled:
            return
        self._cancelled = True
        self._gate._handoffs -= 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - handoffs are runtime-only
            return

        async def _notify_waiters() -> None:
            async with self._gate._condition:
                self._gate._condition.notify_all()

        loop.create_task(_notify_waiters())

    async def __aenter__(self) -> None:
        if self._cancelled or self._consumed:
            raise RuntimeError("shared maintenance handoff is no longer valid")
        self._consumed = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        async with self._gate._condition:
            self._gate._handoffs -= 1
            self._gate._condition.notify_all()


class MaintenanceGate:
    """A fair, task-reentrant asynchronous reader/writer gate.

    Writers fail fast when another writer is pending.  Once a writer declares
    intent, new readers are rejected instead of queued: an update can take
    minutes and retaining incoming chat tasks for that long would produce
    stale, post-restart side effects.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers: dict[asyncio.Task, int] = {}
        self._writer: asyncio.Task | None = None
        self._pending_writer: asyncio.Task | None = None
        self._writer_depth = 0
        self._writer_pending = False
        self._handoffs = 0
        self._poisoned_reason: str | None = None
        self._deferred: list[
            tuple[Callable[[], Awaitable[Any]], asyncio.Future[Any]]
        ] = []

    @property
    def active(self) -> bool:
        return (
            self._poisoned_reason is not None
            or self._writer_pending
            or self._writer is not None
        )

    @property
    def reader_count(self) -> int:
        return len(self._readers) + self._handoffs

    @property
    def writer_task(self) -> asyncio.Task | None:
        """Return the active/pending maintenance writer for shutdown ordering."""
        return self._writer or self._pending_writer

    async def cancel_active_writer(self, timeout: float = 30.0) -> bool:
        """Cancel an update and let its shielded rollback finish before close.

        Runtime supervision calls this *before* it enters closing state so the
        updater can still spawn supervised git children for compensation.
        """
        writer = self.writer_task
        if writer is None or writer.done():
            return True
        if writer is asyncio.current_task():
            return True
        writer.cancel()
        try:
            await _wait_for(asyncio.shield(writer), timeout=max(0.001, timeout))
        except asyncio.CancelledError:
            return True
        except asyncio.TimeoutError:
            return False
        except BaseException:
            # The update task may finish by propagating its original failure;
            # its exclusive-finally/rollback cleanup has nevertheless ended.
            return True
        return True

    def poison(self, reason: str) -> None:
        """Fail-stop runtime work after an unverifiable maintenance rollback."""
        if self._poisoned_reason is None:
            self._poisoned_reason = reason

    def _poison_message(self) -> str:
        return (
            "Robyx maintenance recovery is incomplete; runtime mutations are "
            "disabled until the service is restarted and the recovery marker "
            "is resolved."
        )

    def handoff_shared(self) -> SharedLeaseHandoff:
        """Transfer the caller's lease to work spawned without an await gap.

        This is used by scheduled-delivery watchers: the dispatching scheduler
        already owns a shared lease, and the future watcher must remain a
        reader even if an updater declares writer intent before it first runs.
        """
        task = asyncio.current_task()
        if task is None or (task not in self._readers and self._writer is not task):
            raise RuntimeError("a shared handoff requires an existing lease")
        self._handoffs += 1
        return SharedLeaseHandoff(self)

    async def defer(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Run ``factory`` once the active writer reaches its final data tree.

        Gateway lifecycle events use this instead of being dropped during an
        update. The writer drains queued callbacks before releasing exclusive
        authority; if writer intent vanished before enqueue, the callback runs
        immediately under its own shared lease wrapper.
        """
        loop = asyncio.get_running_loop()
        async with self._condition:
            if self._poisoned_reason is not None:
                raise MaintenanceActiveError(self._poison_message())
            if not self.active:
                future = None
            else:
                future = loop.create_future()
                self._deferred.append((factory, future))
        if future is None:
            return await factory()
        return await asyncio.shield(future)

    async def _drain_deferred(self) -> None:
        """Execute every event queued before exclusive release, exactly once."""
        while True:
            async with self._condition:
                if not self._deferred:
                    return
                factory, future = self._deferred.pop(0)
            try:
                result = await factory()
            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)
                log.error("Deferred maintenance callback failed: %s", exc, exc_info=True)
            else:
                if not future.done():
                    future.set_result(result)

    async def _reject_deferred_after_failed_recovery(self) -> None:
        """Make queued gateway-event loss explicit on a poisoned data tree."""
        async with self._condition:
            queued = self._deferred
            self._deferred = []
        if not queued:
            return
        for _factory, future in queued:
            if not future.done():
                future.set_exception(MaintenanceActiveError(self._poison_message()))
        log.critical(
            "%d deferred gateway event(s) were not applied because maintenance "
            "recovery failed; their waiting dispatch tasks received an error",
            len(queued),
        )

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        """Acquire a shared lease, failing deterministically during updates."""
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always has one here
            raise RuntimeError("maintenance lease requires an asyncio task")

        async with self._condition:
            if self._poisoned_reason is not None:
                raise MaintenanceActiveError(self._poison_message())
            if self._writer is task:
                # The updater itself may call a helper which defensively takes
                # a shared lease.  Exclusive authority already subsumes it.
                yield_directly = True
            elif task in self._readers:
                self._readers[task] += 1
                yield_directly = False
            else:
                if self._writer_pending or self._writer is not None:
                    raise MaintenanceActiveError(MAINTENANCE_MESSAGE)
                self._readers[task] = 1
                yield_directly = False

        try:
            yield
        finally:
            if not yield_directly:
                async with self._condition:
                    depth = self._readers.get(task, 0)
                    if depth <= 1:
                        self._readers.pop(task, None)
                        self._condition.notify_all()
                    else:
                        self._readers[task] = depth - 1

    @asynccontextmanager
    async def exclusive(
        self,
        *,
        quiesce: Callable[[], Awaitable[None]] | None = None,
        finalize: Callable[[], Awaitable[None]] | None = None,
        wait_timeout: float = 30.0,
    ) -> AsyncIterator[None]:
        """Block new readers, quiesce children, then acquire exclusivity.

        ``quiesce`` runs after writer intent is visible but before waiting for
        readers.  Forced updates use it to terminate supervised process groups;
        their delivery watchers then finish and release their shared leases.
        """
        task = asyncio.current_task()
        if task is None:  # pragma: no cover
            raise RuntimeError("maintenance lease requires an asyncio task")

        async with self._condition:
            if self._poisoned_reason is not None:
                raise MaintenanceBusyError(self._poison_message())
            if self._writer is task:
                self._writer_depth += 1
                reentrant = True
            else:
                if task in self._readers:
                    raise MaintenanceBusyError(
                        "cannot upgrade a shared maintenance lease",
                    )
                if self._writer_pending or self._writer is not None:
                    raise MaintenanceBusyError("another update is already running")
                self._writer_pending = True
                self._pending_writer = task
                reentrant = False

        if reentrant:
            try:
                yield
            finally:
                async with self._condition:
                    self._writer_depth -= 1
            return

        acquired = False
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(0.001, float(wait_timeout))
            while True:
                # Re-run quiescence while inherited readers drain. An existing
                # scheduler reader can legitimately spawn a child after the
                # writer's first process snapshot; its lease handoff keeps the
                # writer out, and the next pass drains the newly tracked tree.
                if quiesce is not None:
                    await quiesce()

                async with self._condition:
                    drained = not self._readers and self._handoffs == 0
                    if drained:
                        self._writer = task
                        self._writer_depth = 1
                        self._writer_pending = False
                        self._pending_writer = None
                        break
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise MaintenanceBusyError(
                            "runtime work did not drain before the update deadline",
                        )
                    # With a quiesce callback, poll boundedly so a child born
                    # after the previous drain snapshot is terminated too.
                    interval = min(remaining, 0.1 if quiesce is not None else remaining)
                    try:
                        await _wait_for(
                            self._condition.wait_for(
                                lambda: not self._readers and self._handoffs == 0,
                            ),
                            timeout=interval,
                        )
                    except asyncio.TimeoutError:
                        if loop.time() >= deadline:
                            raise MaintenanceBusyError(
                                "runtime work did not drain before the update deadline",
                            )
                        continue
            acquired = True
            yield
        finally:
            # If quiescence/acquisition failed, lifecycle events may already
            # have queued behind writer intent. Give this task temporary drain
            # authority before clearing intent: the callbacks are ordinary
            # shared work and may coexist with old readers, but no new update
            # may race them and their defensive shared lease must be reentrant.
            drain_owner = acquired
            if not acquired:
                async with self._condition:
                    self._writer = task
                    self._writer_depth = 1
                    self._writer_pending = False
                    self._pending_writer = None
                    drain_owner = True
            try:
                # The update body has now either committed or completed its
                # compensating rollback. Apply gateway events to that final
                # tree, then durably finalise the transaction marker, before
                # restart or another writer is allowed to race them.
                if self._poisoned_reason is None:
                    await self._drain_deferred()
                else:
                    await self._reject_deferred_after_failed_recovery()
                if acquired and finalize is not None:
                    await finalize()
            finally:
                async with self._condition:
                    if drain_owner and self._writer is task:
                        self._writer_depth -= 1
                        if self._writer_depth == 0:
                            self._writer = None
                    if self._pending_writer is task:
                        self._pending_writer = None
                        self._writer_pending = False
                    self._condition.notify_all()


_GATE = MaintenanceGate()


def get_maintenance_gate() -> MaintenanceGate:
    return _GATE


def maintenance_active() -> bool:
    return _GATE.active
