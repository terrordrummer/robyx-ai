"""Process-wide supervision for background tasks and AI subprocesses.

The messaging adapters use different event-loop runners, but they all create
the same two kinds of detached work: long-lived maintenance loops and delivery
watchers for scheduled AI children.  Keeping those objects in one registry
gives shutdown a single, bounded drain point and makes task failures visible.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Callable, Coroutine

log = logging.getLogger("robyx.supervisor")


@dataclass
class _TrackedProcess:
    proc: asyncio.subprocess.Process
    owner: str
    process_group: bool
    # Captured while the group leader is still alive.  Looking it up after
    # ``proc.wait()`` is racy: a leader may exit while one of its children
    # keeps the group alive, at which point ``getpgid(leader_pid)`` fails and
    # the descendant would escape termination.
    pgid: int | None = None


class RuntimeSupervisor:
    """Retain asyncio tasks and terminate registered children on shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._named_tasks: dict[str, asyncio.Task] = {}
        self._processes: dict[int, _TrackedProcess] = {}
        self._terminations: dict[int, asyncio.Task[bool]] = {}
        self._closing = False
        self._shutdown_future: asyncio.Future[None] | None = None

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    @property
    def process_count(self) -> int:
        return len(self._processes)

    @property
    def closing(self) -> bool:
        return self._closing

    def reject_if_closing(self) -> None:
        if self._closing:
            raise RuntimeError("runtime supervisor is shutting down")

    def get_named_task(self, key: str) -> asyncio.Task | None:
        task = self._named_tasks.get(key)
        if task is not None and task.done():
            return None
        return task

    def spawn(
        self,
        coroutine: Coroutine,
        *,
        name: str,
        key: str | None = None,
    ) -> asyncio.Task:
        """Create and retain a task, reporting any unhandled exception.

        A ``key`` makes startup idempotent: while the named task is alive,
        repeated calls return it without constructing duplicate work.  Callers
        that need this behaviour should prefer :meth:`start_once`, whose
        factory avoids creating a coroutine that would then need closing.
        """
        if self._closing:
            coroutine.close()
            raise RuntimeError("runtime supervisor is shutting down")
        if key:
            existing = self.get_named_task(key)
            if existing is not None:
                coroutine.close()
                return existing

        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        if key:
            self._named_tasks[key] = task

        def _on_done(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            if key and self._named_tasks.get(key) is done:
                self._named_tasks.pop(key, None)
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                log.error(
                    "Background task %s crashed: %s",
                    done.get_name(),
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_on_done)
        return task

    def start_once(
        self,
        key: str,
        factory: Callable[[], Coroutine],
        *,
        name: str | None = None,
    ) -> asyncio.Task:
        """Start one live task for ``key`` and return the retained task."""
        existing = self.get_named_task(key)
        if existing is not None:
            return existing
        if self._closing:
            raise RuntimeError("runtime supervisor is shutting down")
        return self.spawn(factory(), name=name or key, key=key)

    def track_process(
        self,
        proc: asyncio.subprocess.Process,
        *,
        owner: str,
        process_group: bool = True,
    ) -> None:
        """Register a child before control returns to an unbounded caller."""
        pid = int(proc.pid)
        existing = self._processes.get(pid)
        if existing is not None:
            if existing.proc is not proc:
                raise RuntimeError("PID %d is already tracked by another process" % pid)
            return
        pgid: int | None = None
        if sys.platform != "win32" and process_group:
            try:
                candidate = os.getpgid(pid)
                if candidate == pid and pid > 1 and candidate != os.getpgrp():
                    pgid = candidate
                else:
                    log.error(
                        "Refusing to track non-isolated process group for PID %d: pgid=%d",
                        pid,
                        candidate,
                    )
            except (ProcessLookupError, OSError):
                log.warning(
                    "Could not capture process group for PID %d (%s)",
                    pid,
                    owner,
                    exc_info=True,
                )
        self._processes[pid] = _TrackedProcess(proc, owner, process_group, pgid)
        try:
            import orphan_tracker

            orphan_tracker.register(pid, owner=owner)
        except Exception as exc:
            log.error(
                "Could not persist child PID %d (%s); terminating fail-closed",
                pid,
                owner,
                exc_info=True,
            )
            self._signal_process(self._processes[pid], signal.SIGTERM)
            raise RuntimeError("child PID registry unavailable") from exc
        if self._closing:
            self._signal_process(self._processes[pid], signal.SIGTERM)
            raise RuntimeError("runtime supervisor is shutting down")

    def untrack_process(
        self,
        proc_or_pid: asyncio.subprocess.Process | int,
        *,
        force: bool = False,
    ) -> bool:
        pid = (
            int(proc_or_pid)
            if isinstance(proc_or_pid, int)
            else int(proc_or_pid.pid)
        )
        tracked = self._processes.get(pid)
        if tracked is not None and not force and self._process_tree_alive(tracked):
            log.warning(
                "Keeping PID %d (%s) tracked because its process group is still alive",
                pid,
                tracked.owner,
            )
            return False
        self._processes.pop(pid, None)
        try:
            import orphan_tracker

            orphan_tracker.unregister(pid)
        except Exception:
            log.debug("Could not remove child PID %d from registry", pid, exc_info=True)
        return True

    @staticmethod
    def _process_group_alive(pgid: int | None) -> bool:
        if sys.platform == "win32" or pgid is None:
            return False
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # The group exists even if the current user cannot signal it.
            return True
        except OSError:
            return False

    @classmethod
    def _process_tree_alive(cls, tracked: _TrackedProcess) -> bool:
        returncode = getattr(tracked.proc, "returncode", None)
        leader_alive = returncode is None or not isinstance(returncode, int)
        return leader_alive or cls._process_group_alive(tracked.pgid)

    def process_tree_alive(self, proc_or_pid: asyncio.subprocess.Process | int) -> bool:
        """Return whether a tracked leader or any member of its group remains."""
        pid = int(proc_or_pid) if isinstance(proc_or_pid, int) else int(proc_or_pid.pid)
        tracked = self._processes.get(pid)
        if tracked is None:
            return False
        return self._process_tree_alive(tracked)

    @staticmethod
    def _signal_process(tracked: _TrackedProcess, sig: signal.Signals) -> None:
        proc = tracked.proc
        pgid = tracked.pgid
        if sys.platform != "win32" and pgid is None and tracked.process_group:
            # Compatibility for directly-constructed tracked records in tests
            # and legacy callers. Production captures this in track_process.
            try:
                candidate = os.getpgid(proc.pid)
                if candidate == proc.pid and candidate > 1 and candidate != os.getpgrp():
                    pgid = candidate
                else:
                    log.error(
                        "Refusing group signal for PID %d: pgid=%d is not isolated",
                        proc.pid,
                        candidate,
                    )
            except (ProcessLookupError, OSError):
                pgid = None
        if sys.platform != "win32" and pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except ProcessLookupError:
                return
            except OSError:
                log.debug("Process-group signal failed for PID %d", proc.pid, exc_info=True)
        returncode = getattr(proc, "returncode", None)
        if isinstance(returncode, int):
            return
        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except ProcessLookupError:
            pass

    async def _wait_for_process_tree(
        self,
        tracked: _TrackedProcess,
        timeout: float,
    ) -> bool:
        """Wait for both the child leader and its captured group to vanish."""
        proc = tracked.proc
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        returncode = getattr(proc, "returncode", None)
        leader_reaped = isinstance(returncode, int)
        waiter: asyncio.Task | None = None
        if not leader_reaped:
            waiter = asyncio.create_task(proc.wait())
            done, pending = await asyncio.wait(
                {waiter},
                timeout=max(0.0, timeout),
            )
            if done:
                try:
                    waiter.result()
                    leader_reaped = True
                except Exception:
                    log.debug("Child wait failed", exc_info=True)
            if pending:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

        # asyncio can reap the direct child; POSIX has no awaitable for
        # non-child group descendants, so poll only the captured group for the
        # unused portion of the bound.
        while leader_reaped and self._process_group_alive(tracked.pgid):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))
        return leader_reaped and not self._process_group_alive(tracked.pgid)

    @staticmethod
    async def _taskkill_tree(pid: int, *, force: bool) -> bool:
        """Ask Windows to terminate a complete descendant tree, bounded."""
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        try:
            child = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(child.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                child.kill()
                await asyncio.gather(child.wait(), return_exceptions=True)
                return False
            except BaseException:
                child.kill()
                await asyncio.gather(child.wait(), return_exceptions=True)
                raise
            return child.returncode == 0
        except (OSError, ProcessLookupError):
            log.warning("Windows taskkill failed for PID %d", pid, exc_info=True)
            return False

    @staticmethod
    def _taskkill_tree_sync(pid: int) -> bool:
        """Synchronous Windows process-tree fallback for signal/atexit paths."""
        import subprocess

        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            log.warning("Windows taskkill fallback failed for PID %d", pid, exc_info=True)
            return False

    async def terminate_process(
        self,
        proc: asyncio.subprocess.Process,
        *,
        grace_seconds: float = 5.0,
    ) -> bool:
        """Terminate one tree to completion even if the caller is cancelled."""
        pid = int(proc.pid)
        in_flight = self._terminations.get(pid)
        if in_flight is None:
            in_flight = asyncio.create_task(
                self._terminate_process_impl(
                    proc,
                    grace_seconds=grace_seconds,
                ),
                name="terminate-process-tree:%d" % pid,
            )
            self._terminations[pid] = in_flight

        cancelled = False
        while True:
            try:
                result = await asyncio.shield(in_flight)
                break
            except asyncio.CancelledError:
                # Mandatory child cleanup is not safely interruptible. Keep
                # waiting for the shared operation, then propagate cancellation.
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _terminate_process_impl(
        self,
        proc: asyncio.subprocess.Process,
        *,
        grace_seconds: float,
    ) -> bool:
        """Single coalesced TERM→KILL→reap operation."""
        pid = int(proc.pid)
        tracked = self._processes.get(pid)
        if tracked is None:
            tracked = _TrackedProcess(proc, "unregistered", False, None)
        stopped = False
        try:
            if sys.platform == "win32" and self._process_tree_alive(tracked):
                tree_command_ok = await self._taskkill_tree(pid, force=False)
                stopped = await self._wait_for_process_tree(tracked, grace_seconds)
                if not stopped:
                    tree_command_ok = (
                        await self._taskkill_tree(pid, force=True)
                        or tree_command_ok
                    )
                    stopped = await self._wait_for_process_tree(tracked, 2.0)
                stopped = stopped and tree_command_ok
            else:
                self._signal_process(tracked, signal.SIGTERM)
                stopped = await self._wait_for_process_tree(tracked, grace_seconds)
                if not stopped:
                    self._signal_process(tracked, signal.SIGKILL)
                    stopped = await self._wait_for_process_tree(tracked, 2.0)
                if not stopped:
                    log.error("Process tree led by PID %d did not stop after SIGKILL", pid)
        finally:
            if stopped:
                self.untrack_process(proc, force=True)
            else:
                log.error(
                    "Keeping live/unreaped process tree PID %d in orphan registry",
                    pid,
                )
            current = asyncio.current_task()
            if self._terminations.get(pid) is current:
                self._terminations.pop(pid, None)
        return stopped

    async def terminate_process_by_pid(
        self,
        pid: int,
        *,
        grace_seconds: float = 5.0,
    ) -> bool:
        """Terminate a tracked tree by lock-file PID.

        Legacy locks may refer to an unregistered child after a bot restart.
        On POSIX we only signal such a process as a group when it is proven to
        be an isolated group leader (``pgid == pid``); shared groups are never
        touched.  Registered processes always use the captured PGID above.
        """
        tracked = self._processes.get(int(pid))
        if tracked is not None:
            return await self.terminate_process(
                tracked.proc,
                grace_seconds=grace_seconds,
            )

        from process import is_pid_alive

        if not is_pid_alive(pid):
            return True
        if sys.platform == "win32":
            tree_command_ok = await self._taskkill_tree(pid, force=False)
            deadline = asyncio.get_running_loop().time() + max(0.0, grace_seconds)
            while is_pid_alive(pid) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            if is_pid_alive(pid):
                tree_command_ok = (
                    await self._taskkill_tree(pid, force=True)
                    or tree_command_ok
                )
                kill_deadline = asyncio.get_running_loop().time() + 2.0
                while is_pid_alive(pid) and asyncio.get_running_loop().time() < kill_deadline:
                    await asyncio.sleep(0.05)
            return tree_command_ok and not is_pid_alive(pid)

        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        if pgid != pid or pid <= 1 or pgid == os.getpgrp():
            # A legacy/restart lock may not have been spawned in its own
            # session.  We cannot safely signal its shared group, but can
            # still terminate the leader through this supervised API.
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            deadline = asyncio.get_running_loop().time() + max(0.0, grace_seconds)
            while is_pid_alive(pid) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            if is_pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    return True
                except OSError:
                    return False
                kill_deadline = asyncio.get_running_loop().time() + 2.0
                while is_pid_alive(pid) and asyncio.get_running_loop().time() < kill_deadline:
                    await asyncio.sleep(0.05)
            return not is_pid_alive(pid)

        def _alive() -> bool:
            return self._process_group_alive(pgid)

        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = asyncio.get_running_loop().time() + max(0.0, grace_seconds)
        while _alive() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if _alive():
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                return True
            kill_deadline = asyncio.get_running_loop().time() + 2.0
            while _alive() and asyncio.get_running_loop().time() < kill_deadline:
                await asyncio.sleep(0.05)
        return not _alive()

    async def _stop_processes(self, *, grace_seconds: float) -> None:
        tracked = list(self._processes.values())
        if not tracked:
            return
        await asyncio.gather(*(
            self.terminate_process(item.proc, grace_seconds=grace_seconds)
            for item in tracked
        ))

    async def drain_processes(self, *, grace_seconds: float = 5.0) -> bool:
        """Stop the current child snapshot without closing the supervisor.

        Updaters use this before taking an on-disk snapshot.  New work remains
        admissible, while concurrent drain/terminate calls for the same PID
        coalesce through :meth:`terminate_process`.
        """
        tracked = list(self._processes.values())
        if not tracked:
            return True
        results = await asyncio.gather(*(
            self.terminate_process(item.proc, grace_seconds=grace_seconds)
            for item in tracked
        ))
        return all(results)

    async def shutdown(
        self,
        *,
        process_grace_seconds: float = 5.0,
        task_grace_seconds: float = 3.0,
        maintenance_writer_grace_seconds: float = 30.0,
    ) -> None:
        """Terminate children, then cancel and drain tasks within a bound."""
        if self._shutdown_future is not None:
            await asyncio.shield(self._shutdown_future)
            return

        loop = asyncio.get_running_loop()
        completed = loop.create_future()
        self._shutdown_future = completed
        try:
            # A cancelled updater may need to spawn git rollback children. Let
            # that critical writer finish before closing rejects new spawns.
            try:
                from maintenance import get_maintenance_gate
                writer_stopped = await get_maintenance_gate().cancel_active_writer(
                    timeout=max(0.0, maintenance_writer_grace_seconds),
                )
                if not writer_stopped:
                    log.error(
                        "Maintenance writer did not finish rollback within %.1fs",
                        maintenance_writer_grace_seconds,
                    )
            except Exception:
                log.error("Maintenance writer shutdown hook failed", exc_info=True)
            self._closing = True
            await self._stop_processes(grace_seconds=process_grace_seconds)
            # Let delivery watchers observe process exit and perform their
            # lock/orphan cleanup before cancellation.
            await asyncio.sleep(0)

            current = asyncio.current_task()
            pending = [
                task for task in self._tasks
                if task is not current and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                _, stubborn = await asyncio.wait(
                    pending,
                    timeout=max(0.0, task_grace_seconds),
                )
                for task in stubborn:
                    log.error(
                        "Background task %s did not stop within %.1fs",
                        task.get_name(),
                        task_grace_seconds,
                    )
            log.info(
                "Runtime supervision stopped (tasks=%d, children=%d)",
                len(self._tasks),
                len(self._processes),
            )
        finally:
            if not completed.done():
                completed.set_result(None)

    def terminate_processes_sync(self, *, grace_seconds: float = 1.0) -> None:
        """Best-effort atexit/signal fallback when no loop can be awaited."""
        tracked = list(self._processes.values())
        if sys.platform == "win32":
            for item in tracked:
                if self._taskkill_tree_sync(int(item.proc.pid)):
                    self.untrack_process(item.proc, force=True)
            return
        for item in tracked:
            self._signal_process(item, signal.SIGTERM)
        if not tracked:
            return

        from process import is_pid_alive

        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline:
            if not any(self._process_tree_alive(item) for item in tracked):
                break
            time.sleep(0.05)
        for item in tracked:
            if self._process_tree_alive(item):
                self._signal_process(item, signal.SIGKILL)
            else:
                self.untrack_process(item.proc, force=True)

    def reset_for_tests(self) -> None:
        """Restore an empty reusable supervisor after a fully drained test."""
        if any(not task.done() for task in self._tasks) or self._processes:
            raise RuntimeError("cannot reset a live runtime supervisor")
        self._tasks.clear()
        self._named_tasks.clear()
        self._terminations.clear()
        self._closing = False
        self._shutdown_future = None


_SUPERVISOR = RuntimeSupervisor()


def get_runtime_supervisor() -> RuntimeSupervisor:
    return _SUPERVISOR
