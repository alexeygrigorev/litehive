"""Workspace locking, runner guards, and mutation helpers."""

import logging
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, TextIO, cast

from litehive.container import build_workspace
from litehive.config.workspace_files import workspace_dir
from litehive.domain.common import RunnerStatus, TaskExecutionStatus, utcnow
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.state.lock_manager import WorkspaceLockManager
from litehive.state.process_lock import ProcessLockManager
from litehive.state.store import runtime_store

from litehive.tasks.constants import (
    HEARTBEAT_LATE_THRESHOLD_SECONDS,
    MISSING,
    RUNNER_LOCKS,
    RUNNER_LOCKS_MUTEX,
)
from litehive.domain.task_ops import WorkspaceConflictError, RunnerLockState
from litehive.tasks.paths import runner_lock_path

if TYPE_CHECKING:
    from litehive.tasks.audit import TaskAuditEntry
    from litehive.workspace import Workspace

logger = logging.getLogger(__name__)


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


def _runner_lock_manager(
    root: Path,
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
        lock_path=runner_lock_path(root),
        process_name="runner",
        pid_is_alive=runner_pid_is_alive,
        held_in_process=held_in_process,
    )


@contextmanager
def workspace_lock(root: Path):
    """
    Hold the workspace-level flock for short, blocking critical sections.

    Used by callers that mutate workspace state without owning the
    long-lived runner guard (CLI mutations, recovery probes, state
    repair); blocking acquisition is intentional so a second writer waits
    rather than failing.
    """
    lock_path = workspace_dir(root) / ".lock"
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
    who owns the workspace; the metadata is what makes
    ``runner_conflict_message`` informative instead of just "lock held".
    """
    ProcessLockManager(
        lock_path=Path(handle.name),
        process_name="runner",
        pid_is_alive=runner_pid_is_alive,
    ).write_locked_metadata(
        handle,
        status.model_dump(mode="json"),
    )


def _save_runner_process_state(root: Path, status: RunnerStatusState) -> None:
    """
    Mirror runner status into the SQLite runtime store.

    Lets dashboards and read-only consumers see runner identity without
    taking the flock; the lockfile remains the source of truth, but the
    SQLite mirror is what the web dashboard polls.
    """
    runtime_store(root).save_process_state(
        "runner",
        status=status.status or RunnerStatus.RUNNING,
        payload=status.model_dump(mode="json"),
    )


def _clear_runner_process_state(root: Path) -> None:
    """
    Drop the SQLite runner-status mirror after the lockfile is released.

    Without the explicit clear, dashboards would keep reporting a phantom
    runner identity after the real process has gone; the lockfile-side
    metadata is gone but the SQLite mirror would survive across restarts.
    """
    runtime_store(root).clear_process_state("runner")


def read_runner_lock_metadata(root: Path) -> RunnerStatusState:
    """
    Read recorded runner identity without taking the flock.

    Returns an empty ``RunnerStatusState`` when nothing is recorded; the
    non-blocking read is what lets status surfaces probe ownership without
    contending with a live runner that holds the lock.
    """
    data = _runner_lock_manager(root.resolve()).read_metadata(strict=True)
    if data is None:
        return RunnerStatusState()
    return RunnerStatusState.model_validate(data)


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


def runner_lock_is_active(root: Path) -> bool:
    """
    Probe whether any process currently holds the runner flock.

    The in-process check avoids self-deadlock when a runner queries its
    own status (flock is per-file-descriptor, so a second probe from the
    same process would otherwise succeed and confuse ownership tracking).
    """
    root = root.resolve()
    return _runner_lock_manager(root, held_in_process=lambda: root in RUNNER_LOCKS).is_active()


def runner_status_needs_reconciliation(root: Path) -> bool:
    """
    Detect leftover "running" markers when no runner holds the lock.

    Lets the status reporter surface STALE instead of falsely reporting
    idle: an active_task_id pointer or a running ``execution_status``
    without a matching live runner is the unambiguous signal that a prior
    runner crashed and left the workspace wedged.
    """
    # inline: state.records and state.persist top-level-import state.locking (would cycle).
    from litehive.state.records import list_tasks  # noqa: PLC0415
    from litehive.state.persist import load_state  # noqa: PLC0415

    state = load_state(root)
    if state.active_task_id is not None:
        return True
    return any(
        task.runtime.pipeline.execution_status == TaskExecutionStatus.RUNNING
        for task in list_tasks(root, strict=False)
    )


def clear_runner_lock_metadata(root: Path) -> None:
    """
    Wipe stale lockfile metadata when no runner is alive.

    Called by the status reporter once it has confirmed the recorded PID
    is gone; without the cleanup the next status query would still see
    the dead runner's identity and spend cycles re-reconciling nothing.
    """
    root = root.resolve()
    manager = _runner_lock_manager(root, held_in_process=lambda: root in RUNNER_LOCKS)
    if manager.clear_metadata_if_unlocked():
        _clear_runner_process_state(root)


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


def runner_status(root: Path) -> RunnerStatusState:
    """
    Resolve the workspace's authoritative runner status.

    Returns RUNNING/LATE/STALE/empty and opportunistically clears
    lockfile metadata when the runner is gone but workspace state is
    already clean. The canonical entry point used by the CLI status
    command and the runner guard before it tries to acquire.
    """
    root = root.resolve()
    status = read_runner_lock_metadata(root)
    if runner_lock_is_active(root):
        if heartbeat_is_late(status.heartbeat_at):
            return status.model_copy(update={"status": RunnerStatus.LATE})
        return status.model_copy(update={"status": RunnerStatus.RUNNING})
    if not runner_metadata_present(status):
        return RunnerStatusState()
    if runner_status_needs_reconciliation(root):
        return status.model_copy(update={"status": RunnerStatus.STALE})
    clear_runner_lock_metadata(root)
    return RunnerStatusState()


def runner_status_readonly(root: Path) -> RunnerStatusState:
    """
    Return runner status using only file reads and PID checks.

    No ``fcntl.flock`` calls so it is safe for read-only consumers like
    the web dashboard that must never contend with the runner lock; the
    tradeoff is that "stale but flock-held" cannot be distinguished from
    "actively running".
    """
    root = root.resolve()
    status = read_runner_lock_metadata(root)
    if not runner_metadata_present(status):
        return RunnerStatusState()
    # If the recorded PID is alive, the runner is (or was recently) active.
    if runner_pid_is_alive(status.pid):
        if heartbeat_is_late(status.heartbeat_at):
            return status.model_copy(update={"status": RunnerStatus.LATE})
        return status.model_copy(update={"status": RunnerStatus.RUNNING})
    # PID is gone — metadata is stale.
    return status.model_copy(update={"status": RunnerStatus.STALE})


def touch_runner_status(
    root: Path,
    active_task_id: str | None | object = MISSING,
) -> None:
    """
    Refresh heartbeat (and optionally active task pointer) under the metadata mutex.

    Used by the heartbeat thread and by stage transitions that change
    which task the runner is currently executing; the mutex is what stops
    the background heartbeat from racing the foreground stage transition
    when both write the lockfile.
    """
    root = root.resolve()
    lock_state = RUNNER_LOCKS.get(root)
    if lock_state is None:
        return
    with lock_state.metadata_lock:
        lock_state.status.heartbeat_at = utcnow()
        lock_state.status.status = RunnerStatus.RUNNING
        if active_task_id is not MISSING:
            lock_state.status.active_task_id = cast(str | None, active_task_id)
        write_runner_lock_metadata(lock_state.handle, lock_state.status)
        _save_runner_process_state(root, lock_state.status)


@contextmanager
def runner_heartbeat(
    root: Path,
    active_task_id: str | None = None,
    interval_seconds: float = 1.0,
):
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
        Background-thread body refreshing ``runner_status`` on a fixed cadence.

        Loops every ``interval_seconds`` until ``stop_event`` fires; held
        as a named closure so the daemon thread shows up by name in stack
        traces and the ``stop_event``/``active_task_id`` captures are
        explicit rather than implicit lambdas.
        """
        while not stop_event.wait(interval_seconds):
            touch_runner_status(root, active_task_id=active_task_id)

    touch_runner_status(root, active_task_id=active_task_id)
    thread = threading.Thread(target=_heartbeat_loop, name="litehive-runner-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=max(interval_seconds, 0.1) * 2)
        touch_runner_status(root, active_task_id=None)


def current_thread_owns_runner_guard(root: Path) -> bool:
    """
    Distinguish reentrant calls from foreign threads.

    Required because the runner uses one thread to acquire the guard and
    another to read status; nested mutation guards check this before
    re-locking so a recursive transition does not deadlock against itself.
    """
    root = root.resolve()
    owner_thread_id = threading.get_ident()
    with RUNNER_LOCKS_MUTEX:
        existing = RUNNER_LOCKS.get(root)
    return existing is not None and existing.owner_thread_id == owner_thread_id


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


def runner_lock_pid_is_stale(root: Path) -> bool:
    """
    True when the runner lock records a PID that is no longer alive.

    Used by stale-runner recovery as the unambiguous "previous owner is
    dead" signal so the new runner can safely take the lock without
    racing a still-live predecessor.
    """
    return _runner_lock_manager(root.resolve()).pid_is_stale()


def runner_lock_is_held(root: Path) -> bool:
    """
    Probe whether this thread or another process holds the runner guard.

    Used by mutation paths that must reject (foreign holder) or fall
    through (own holder); the same probe answers both cases via the
    in-process check that ``_runner_lock_manager`` is wired with.
    """
    root = root.resolve()
    return _runner_lock_manager(root, held_in_process=lambda: current_thread_owns_runner_guard(root)).is_active()


def runner_conflict_message(root: Path) -> str:
    """
    Build the human-readable message attached to ``WorkspaceConflictError``.

    Operators must see who is holding the workspace lock so they can
    decide whether to wait, kill, or repair; consumed by the runner guard
    and by CLI mutation commands when the lock is busy.
    """
    metadata = read_runner_lock_metadata(root)
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


@contextmanager
def workspace_runner_guard(root: Path):
    """
    Long-lived workspace guard wrapping a runner's whole lifetime.

    Takes the exclusive flock, auto-repairs stale state left by a crashed
    predecessor, and supports reentry from the same thread so nested
    mutation guards work without deadlocking. The single canonical
    entry-point — every other guard helper layers on top of this.
    """
    root = root.resolve()
    workspace = build_workspace(root)
    owner_thread_id = threading.get_ident()
    manager = _runner_lock_manager(root, held_in_process=lambda: root in RUNNER_LOCKS)
    with RUNNER_LOCKS_MUTEX:
        existing = RUNNER_LOCKS.get(root)
        if existing is not None:
            if existing.owner_thread_id != owner_thread_id:
                raise WorkspaceConflictError(runner_conflict_message(root))
            existing.depth += 1
    if existing is not None:
        try:
            yield
        finally:
            with RUNNER_LOCKS_MUTEX:
                lock_state = RUNNER_LOCKS[root]
                if lock_state.depth <= 1:
                    RUNNER_LOCKS.pop(root, None)
                    should_close = True
                else:
                    lock_state.depth -= 1
                    should_close = False
            if should_close:
                manager.lock_manager.release(lock_state.handle, clear_metadata=True)
                _clear_runner_process_state(root)
        return

    handle: TextIO | None = None
    try:
        try:
            handle = manager.lock_manager.acquire(nonblocking=True)
        except BlockingIOError as exc:
            raise WorkspaceConflictError(runner_conflict_message(root)) from exc
        # Auto-repair stale state left by a crashed runner.  We hold the
        # exclusive flock, so no other runner is alive — any active_task_id
        # or "running" execution_status is leftover from a dead process.
        _auto_repair_stale_state(workspace)
        now = utcnow()
        status = RunnerStatusState(
            status=RunnerStatus.RUNNING,
            pid=os.getpid(),
            workspace=str(root),
            command=" ".join(sys.argv),
            started_at=now,
            heartbeat_at=now,
        )
        write_runner_lock_metadata(handle, status)
        _save_runner_process_state(root, status)
        with RUNNER_LOCKS_MUTEX:
            RUNNER_LOCKS[root] = RunnerLockState(handle=handle, depth=1, status=status, owner_thread_id=owner_thread_id)
    except (OSError, RuntimeError, ValueError):
        if handle is not None and not handle.closed:
            handle.close()
        raise

    try:
        yield
    finally:
        with RUNNER_LOCKS_MUTEX:
            RUNNER_LOCKS.pop(root, None)
        manager.lock_manager.release(handle, clear_metadata=True)
        _clear_runner_process_state(root)


@contextmanager
def workspace_mutation_guard(root: Path):
    """
    Wrap individual workspace writes with reentrant guard semantics.

    A no-op when the current thread already owns the runner guard;
    otherwise acquires the runner guard for the duration of the
    mutation. CLI mutation commands rely on this so they can run
    correctly with or without an active runner.
    """
    root = root.resolve()
    owner_thread_id = threading.get_ident()
    with RUNNER_LOCKS_MUTEX:
        existing = RUNNER_LOCKS.get(root)
    if existing is not None and existing.owner_thread_id == owner_thread_id:
        yield
        return
    with workspace_runner_guard(root):
        yield


def ensure_future_task_mutation_allowed(
    root: Path,
    task_ids: list[str],
    state: WorkspaceState | None = None,
) -> None:
    """
    Guard CLI edits against tasks the runner is actively driving.

    Raises ``WorkspaceConflictError`` when the user tries to mutate a
    task that is currently executing. Called by ``litehive update``,
    queue reorder, and similar mutation entrypoints so an in-flight
    transition cannot have its inputs changed mid-run.
    """
    # inline: state.records / tasks.queue top-level-import state.locking (would cycle).
    from litehive.state.records import get_task  # noqa: PLC0415
    from litehive.tasks.queue import is_task_eligible_for_execution, active_task_markers  # noqa: PLC0415

    markers = active_task_markers(root, state)
    conflicts: list[str] = []
    for task_id in task_ids:
        if task_id not in markers:
            continue
        task = get_task(root, task_id)
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
    root: Path,
    task: TaskRecord,
    journal_message: str | None = None,
    audit_entries: list["TaskAuditEntry"] | None = None,
) -> None:
    """
    Save a single task edit without touching workspace-level state.

    Used by CLI edit commands after ``ensure_future_task_mutation_allowed``
    has cleared the change; preserves the workspace queue/counters so a
    metadata edit cannot accidentally reorder runtime state in the same
    transaction.
    """
    # inline: state.records top-level-imports state.locking (would cycle).
    from litehive.state.records import ensure_runtime_ignored, task_state_for_storage  # noqa: PLC0415

    task.updated_at = utcnow()
    if journal_message is None:
        task_journal_messages = None
    else:
        task_journal_messages = {task.id: journal_message}
    runtime_store(root).save_runtime_transaction(
        task_intents={task.id: task.to_intent_record()},
        task_states={task.id: task_state_for_storage(task)},
        task_journal_messages=task_journal_messages,
        audit_entries=audit_entries,
    )
    ensure_runtime_ignored(root)
