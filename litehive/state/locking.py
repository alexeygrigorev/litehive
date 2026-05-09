"""Workspace locking, runner guards, and mutation helpers."""

import logging
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, TextIO, cast

from litehive.domain.common import RunnerStatus, TaskExecutionStatus, utcnow
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.state.lock_manager import WorkspaceLockManager
from litehive.state.process_lock import ProcessLockManager
from litehive.state.store import RuntimeStore
from litehive.workspace import Workspace

from litehive.tasks.constants import (
    HEARTBEAT_LATE_THRESHOLD_SECONDS,
    MISSING,
    RUNNER_LOCKS,
    RUNNER_LOCKS_MUTEX,
)
from litehive.domain.task_ops import WorkspaceConflictError, RunnerLockState

if TYPE_CHECKING:
    from litehive.tasks.audit import TaskAuditEntry

logger = logging.getLogger(__name__)


def _runner_lock_key_impl(workspace: Workspace) -> Path:
    """
    Return the normalized key for the in-process runner-lock registry.
    """
    return workspace.root.resolve()


def _pid_is_zombie(pid: int) -> bool:
    """
    Distinguish a defunct (zombie) process from a live one.

    Reaped-but-not-yet-cleaned PIDs would otherwise pass the simple
    ``os.kill(pid, 0)`` liveness probe, so the runner-conflict checks would
    falsely report a stale lock as held; reading ``/proc/<pid>/stat`` is
    the cheapest way to tell the two apart on Linux.
    """
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        fields = stat_path.read_text(encoding="utf-8").split()
    except OSError:
        return False
    return len(fields) > 2 and fields[2] == "Z"


def _runner_lock_manager_impl(
    workspace: Workspace,
    held_in_process: Callable[[], bool] | None = None,
) -> ProcessLockManager:
    """
    Build the ``ProcessLockManager`` for the workspace runner lock.

    Every site that touches the lockfile reaches it through this helper so
    the liveness/identity policy (PID checks, in-process detection, fsync
    settings) lives in one place rather than being re-derived in every
    caller.
    """
    return ProcessLockManager(
        process_name="runner",
        lock_manager=WorkspaceLockManager(
            path=workspace.runtime_path("runtime", ".runner.lock"),
            pid_is_alive=runner_pid_is_alive,
            held_in_process=held_in_process,
        ),
        runtime_store=RuntimeStore(workspace),
    )


@dataclass(frozen=True, slots=True)
class WorkspaceRunnerLock:
    """
    Workspace-bound runner lock API.

    This is the object boundary for runner lockfile behavior. Existing
    free functions are being migrated in small slices so each caller can
    be verified before the corresponding wrapper is deleted.
    """

    workspace: Workspace

    def read_metadata(self) -> RunnerStatusState:
        data = _runner_lock_manager_impl(self.workspace).read_metadata(strict=True)
        if data is None:
            return RunnerStatusState()
        return RunnerStatusState.model_validate(data)

    def is_active(self) -> bool:
        lock_key = _runner_lock_key_impl(self.workspace)
        return _runner_lock_manager_impl(
            self.workspace,
            held_in_process=lambda: lock_key in RUNNER_LOCKS,
        ).is_active()

    def clear_metadata(self) -> None:
        lock_key = _runner_lock_key_impl(self.workspace)
        manager = _runner_lock_manager_impl(
            self.workspace,
            held_in_process=lambda: lock_key in RUNNER_LOCKS,
        )
        if manager.clear_metadata_if_unlocked():
            self._clear_process_state()

    def status(self) -> RunnerStatusState:
        status = self.read_metadata()
        if self.is_active():
            if heartbeat_is_late(status.heartbeat_at):
                return status.model_copy(update={"status": RunnerStatus.LATE})
            return status.model_copy(update={"status": RunnerStatus.RUNNING})
        if not runner_metadata_present(status):
            return RunnerStatusState()
        if self.needs_reconciliation():
            return status.model_copy(update={"status": RunnerStatus.STALE})
        self.clear_metadata()
        return RunnerStatusState()

    def needs_reconciliation(self) -> bool:
        """
        Detect leftover running markers when no runner holds the lock.
        """
        # inline: state.records and state.persist top-level-import state.locking (would cycle).
        from litehive.state.persist import WorkspaceStateRepository  # noqa: PLC0415
        from litehive.state.records import WorkspaceTasks  # noqa: PLC0415

        state = WorkspaceStateRepository(self.workspace).load()
        if state.active_task_id is not None:
            return True
        return any(
            task.runtime.pipeline.execution_status == TaskExecutionStatus.RUNNING
            for task in WorkspaceTasks(self.workspace).list(strict=False)
        )

    def owns_current_thread(self) -> bool:
        lock_key = _runner_lock_key_impl(self.workspace)
        owner_thread_id = threading.get_ident()
        with RUNNER_LOCKS_MUTEX:
            existing = RUNNER_LOCKS.get(lock_key)
        return existing is not None and existing.owner_thread_id == owner_thread_id

    def is_held(self) -> bool:
        return _runner_lock_manager_impl(
            self.workspace,
            held_in_process=self.owns_current_thread,
        ).is_active()

    def pid_is_stale(self) -> bool:
        return _runner_lock_manager_impl(self.workspace).pid_is_stale()

    def touch(self, active_task_id: str | None | object = MISSING) -> None:
        lock_key = _runner_lock_key_impl(self.workspace)
        lock_state = RUNNER_LOCKS.get(lock_key)
        if lock_state is None:
            return
        with lock_state.metadata_lock:
            lock_state.status.heartbeat_at = utcnow()
            lock_state.status.status = RunnerStatus.RUNNING
            if active_task_id is not MISSING:
                lock_state.status.active_task_id = cast(str | None, active_task_id)
            write_runner_lock_metadata(lock_state.handle, lock_state.status)
            self._save_process_state(lock_state.status)

    @contextmanager
    def heartbeat(
        self,
        active_task_id: str | None = None,
        interval_seconds: float = 1.0,
    ) -> Iterator[None]:
        """
        Run a background thread refreshing the runner heartbeat while a task runs.

        Entered by the runner around each task so the status reporter doesn't
        escalate a busy runner to LATE just because the foreground thread is
        blocked on a long subagent call; the daemon thread name keeps stack
        traces stable when debugging.
        """
        stop_event = threading.Event()

        def _heartbeat_loop() -> None:
            """
            Background-thread body refreshing runner status on a fixed cadence.
            """
            while not stop_event.wait(interval_seconds):
                self.touch(active_task_id=active_task_id)

        self.touch(active_task_id=active_task_id)
        thread = threading.Thread(target=_heartbeat_loop, name="litehive-runner-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=max(interval_seconds, 0.1) * 2)
            self.touch(active_task_id=None)

    def conflict_message(self) -> str:
        metadata = self.read_metadata()
        pid = metadata.pid
        started_at = metadata.started_at
        heartbeat_at = metadata.heartbeat_at
        command = metadata.command
        details = []
        if pid is not None:
            details.append(f"pid={pid}")
        if started_at:
            details.append(f"started_at={started_at}")
        if heartbeat_at:
            details.append(f"heartbeat_at={heartbeat_at}")
        if command:
            details.append(f"command={command}")
        if details:
            suffix = f" ({', '.join(details)})"
        else:
            suffix = ""
        return (
            f"workspace is already being mutated by another runner{suffix}. "
            "Wait for the active run to finish before changing this workspace."
        )

    @contextmanager
    def guard(self) -> Iterator[None]:
        """
        Long-lived workspace guard wrapping a runner's whole lifetime.

        Takes the exclusive flock, auto-repairs stale state left by a crashed
        predecessor, and supports reentry from the same thread so nested
        mutation guards work without deadlocking.
        """
        lock_key = _runner_lock_key_impl(self.workspace)
        owner_thread_id = threading.get_ident()
        manager = _runner_lock_manager_impl(
            self.workspace,
            held_in_process=lambda: lock_key in RUNNER_LOCKS,
        )
        with RUNNER_LOCKS_MUTEX:
            existing = RUNNER_LOCKS.get(lock_key)
            if existing is not None:
                if existing.owner_thread_id != owner_thread_id:
                    raise WorkspaceConflictError(self.conflict_message())
                existing.depth += 1
        if existing is not None:
            try:
                yield
            finally:
                with RUNNER_LOCKS_MUTEX:
                    lock_state = RUNNER_LOCKS[lock_key]
                    if lock_state.depth <= 1:
                        RUNNER_LOCKS.pop(lock_key, None)
                        should_close = True
                    else:
                        lock_state.depth -= 1
                        should_close = False
                if should_close:
                    manager.lock_manager.release(lock_state.handle, clear_metadata=True)
                    self._clear_process_state()
            return

        handle: TextIO | None = None
        try:
            try:
                handle = manager.lock_manager.acquire(nonblocking=True)
            except BlockingIOError as exc:
                raise WorkspaceConflictError(self.conflict_message()) from exc
            _auto_repair_stale_state(self.workspace)
            now = utcnow()
            status = RunnerStatusState(
                status=RunnerStatus.RUNNING,
                pid=os.getpid(),
                workspace=str(lock_key),
                command=" ".join(sys.argv),
                started_at=now,
                heartbeat_at=now,
            )
            write_runner_lock_metadata(handle, status)
            self._save_process_state(status)
            with RUNNER_LOCKS_MUTEX:
                RUNNER_LOCKS[lock_key] = RunnerLockState(
                    handle=handle,
                    depth=1,
                    status=status,
                    owner_thread_id=owner_thread_id,
                )
        except (OSError, RuntimeError, ValueError):
            if handle is not None and not handle.closed:
                handle.close()
            raise

        try:
            yield
        finally:
            with RUNNER_LOCKS_MUTEX:
                RUNNER_LOCKS.pop(lock_key, None)
            manager.lock_manager.release(handle, clear_metadata=True)
            self._clear_process_state()

    def _save_process_state(self, status: RunnerStatusState) -> None:
        RuntimeStore(self.workspace).save_process_state(
            "runner",
            status=status.status or RunnerStatus.RUNNING,
            payload=status.model_dump(mode="json"),
        )

    def _clear_process_state(self) -> None:
        RuntimeStore(self.workspace).clear_process_state("runner")


@dataclass(frozen=True, slots=True)
class WorkspaceMutationGuard:
    """
    Workspace-bound short mutation guard.
    """

    workspace: Workspace

    def is_owned_by_current_thread(self) -> bool:
        return WorkspaceRunnerLock(self.workspace).owns_current_thread()

    @contextmanager
    def hold(self) -> Iterator[None]:
        if self.is_owned_by_current_thread():
            yield
            return
        with WorkspaceRunnerLock(self.workspace).guard():
            yield

    def ensure_future_task_mutation_allowed(
        self,
        task_ids: list[str],
        state: WorkspaceState | None = None,
    ) -> None:
        # inline: tasks.queue top-level-imports state.locking (would cycle).
        from litehive.tasks.queue import is_task_eligible_for_execution, TaskQueueService  # noqa: PLC0415
        from litehive.state.records import WorkspaceTasks  # noqa: PLC0415

        tasks = WorkspaceTasks(self.workspace)
        markers = TaskQueueService(self.workspace).active_task_markers(state)
        conflicts: list[str] = []
        for task_id in task_ids:
            if task_id not in markers:
                continue

            task = tasks.get(task_id)
            marker_set = set(markers[task_id])
            if (
                marker_set == {"workspace.active_task_id"}
                and task is not None
                and not is_task_eligible_for_execution(task)
                and task.runtime.pipeline.execution_status != TaskExecutionStatus.RUNNING
            ):
                continue
            if (
                marker_set == {"task.status=in_progress"}
                and task is not None
                and task.runtime.pipeline.execution_status != TaskExecutionStatus.RUNNING
            ):
                continue
            conflicts.append(f"{task_id} ({', '.join(markers[task_id])})")
        if conflicts:
            details = "; ".join(conflicts)
            raise WorkspaceConflictError(
                f"runner is actively using task state that cannot be changed concurrently: {details}"
            )

    def persist_future_task_update(
        self,
        task: TaskRecord,
        journal_message: str | None = None,
        audit_entries: list["TaskAuditEntry"] | None = None,
    ) -> None:
        # inline: state.records top-level-imports state.locking (would cycle).
        from litehive.state.records import task_state_for_storage, WorkspaceTasks  # noqa: PLC0415

        task.updated_at = utcnow()
        if journal_message is None:
            task_journal_messages = None
        else:
            task_journal_messages = {task.id: journal_message}
        RuntimeStore(self.workspace).save_runtime_transaction(
            task_intents={task.id: task.to_intent_record()},
            task_states={task.id: task_state_for_storage(task)},
            task_journal_messages=task_journal_messages,
            audit_entries=audit_entries,
        )
        WorkspaceTasks(self.workspace).ensure_runtime_ignored()


@dataclass(frozen=True, slots=True)
class WorkspaceStateLock:
    """
    Workspace-bound short blocking flock for state mutations.

    Used by callers that mutate workspace state without owning the
    long-lived runner guard (CLI mutations, recovery probes, state
    repair); blocking acquisition is intentional so a second writer waits
    rather than failing.
    """

    workspace: Workspace

    @contextmanager
    def hold(self) -> Iterator[None]:
        lock_path = self.workspace.control_dir() / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w", encoding="utf-8") as handle:
            manager = WorkspaceLockManager(lock_path, pid_is_alive=runner_pid_is_alive)
            manager.lock(handle, nonblocking=False)
            try:
                yield
            finally:
                manager.unlock(handle)


def write_runner_lock_metadata(handle: TextIO, status: RunnerStatusState) -> None:
    """
    Persist runner identity into the held lockfile.

    Records pid, command, and heartbeat so other processes can diagnose
    who owns the workspace; the metadata is what makes runner lock conflict
    messages informative instead of just "lock held".
    """
    ProcessLockManager(
        process_name="runner",
        lock_manager=WorkspaceLockManager(
            path=Path(handle.name),
            pid_is_alive=runner_pid_is_alive,
        ),
    ).write_locked_metadata(
        handle,
        status.model_dump(mode="json"),
    )


def runner_metadata_present(status: RunnerStatusState) -> bool:
    """
    Tell apart a fresh ``RunnerStatusState`` from one that actually carries data.

    Reconciliation logic uses this so it only fires when there is something
    to reconcile; a default-constructed status with no identifying fields
    means the lockfile was never claimed and there's nothing to clean.
    """
    return any(
        (
            status.pid is not None,
            bool(status.workspace),
            bool(status.command),
            status.started_at is not None,
            status.heartbeat_at is not None,
            status.active_task_id is not None,
        )
    )


def heartbeat_is_late(heartbeat_at: str | None) -> bool:
    """
    Decide whether a recorded heartbeat is old enough to mark the runner LATE.

    Tolerant of missing or malformed timestamps (returns ``False`` rather
    than raising) so a corrupt lockfile never panics the status reporter
    mid-render; the threshold is ``HEARTBEAT_LATE_THRESHOLD_SECONDS``.
    """
    if heartbeat_at is None:
        return False
    try:
        from datetime import UTC, datetime  # noqa: PLC0415

        ts = datetime.fromisoformat(heartbeat_at)
        age = (datetime.now(UTC) - ts).total_seconds()
        return age > HEARTBEAT_LATE_THRESHOLD_SECONDS
    except (ValueError, TypeError):
        return False


def runner_pid_is_alive(pid: object) -> bool:
    """
    The single liveness oracle used by every lock-related code path.

    Centralised so PID checks (zombie handling, permission edge cases,
    malformed values) behave identically across the codebase; subtle
    divergence between callers in the past produced confusing "stale lock
    held by alive process" reports.
    """
    if not isinstance(pid, (int, str, bytes, float)):
        return False
    try:
        candidate = int(pid)
    except (TypeError, ValueError):
        return False
    if candidate <= 0:
        return False
    try:
        os.kill(candidate, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _pid_is_zombie(candidate)


def subagent_process_is_stale(task: "TaskRecord") -> bool:
    """
    True when the task records a subagent PID that is no longer alive.

    Consulted by stale-runner recovery to distinguish "runner crashed"
    from "subagent crashed under a still-live runner"; only fires when
    the runtime claims the task is running, so a deliberately-finished
    subagent does not look stale.
    """
    if task.runtime.pipeline.execution_status != TaskExecutionStatus.RUNNING:
        return False
    active = task.runtime.execution.active_subagent
    if active is None or active.pid is None:
        return False
    return not runner_pid_is_alive(active.pid)


def _auto_repair_stale_state(workspace: "Workspace") -> None:
    """
    Clear stale ``active_task_id`` and running execution statuses on startup.

    Called inside ``workspace_runner_guard`` after acquiring the
    exclusive flock; since we hold the lock no other runner is alive, so
    any ``active_task_id`` or ``execution_status == "running"`` is
    leftover from a crashed process and must be reconciled before the
    new runner starts.
    """
    # inline: recovery.workspace_repair top-level-imports state.locking (would cycle).
    from litehive.recovery.workspace_repair import repair_workspace_state  # noqa: PLC0415

    try:
        result = repair_workspace_state(workspace)
        if result.mutated:
            import sys  # noqa: PLC0415

            print(
                f"auto-repair: cleared stale state "
                f"(active={result.cleared_active_task_id or '-'}, "
                f"requeued={', '.join(result.requeued_task_ids) or '-'})",
                file=sys.stderr,
            )
    except (OSError, sqlite3.DatabaseError, RuntimeError, ValueError):
        logger.exception("auto-repair of stale runner state failed")
