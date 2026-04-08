"""Workspace locking, runner guards, and mutation helpers."""

import fcntl
import logging
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

import yaml

from litehive.config import workspace_dir
from litehive.models import RunnerStatusState, TaskRecord, WorkspaceState, utcnow

from .constants import (
    HEARTBEAT_LATE_THRESHOLD_SECONDS,
    _MISSING,
    _RUNNER_LOCKS,
    _RUNNER_LOCKS_MUTEX,
)
from .models import WorkspaceConflictError, _RunnerLockState
from .paths import runner_lock_path, task_dir, task_file, task_runtime_file

logger = logging.getLogger(__name__)


@contextmanager
def _workspace_lock(root: Path):
    lock_path = workspace_dir(root) / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_runner_lock_metadata(handle: TextIO, status: RunnerStatusState) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(yaml.safe_dump(status.model_dump(mode="python"), sort_keys=False))
    handle.flush()


def _read_runner_lock_metadata(root: Path) -> RunnerStatusState:
    lock_path = runner_lock_path(root)
    if not lock_path.exists():
        return RunnerStatusState()
    data = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return RunnerStatusState()
    return RunnerStatusState(**data)


def _runner_metadata_present(status: RunnerStatusState) -> bool:
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


def _runner_lock_is_active(root: Path) -> bool:
    root = root.resolve()
    if root in _RUNNER_LOCKS:
        return True
    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def _runner_status_needs_reconciliation(root: Path) -> bool:
    from .crud import list_tasks
    from .persistence import load_state

    state = load_state(root)
    if state.active_task_id is not None:
        return True
    return any(task.runtime.execution_status == "running" for task in list_tasks(root))


def _clear_runner_lock_metadata(root: Path) -> None:
    root = root.resolve()
    if root in _RUNNER_LOCKS:
        return
    lock_path = runner_lock_path(root)
    if not lock_path.exists():
        return
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        handle.seek(0)
        handle.truncate()
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _heartbeat_is_late(heartbeat_at: str | None) -> bool:
    if heartbeat_at is None:
        return False
    try:
        from datetime import UTC, datetime

        ts = datetime.fromisoformat(heartbeat_at)
        age = (datetime.now(UTC) - ts).total_seconds()
        return age > HEARTBEAT_LATE_THRESHOLD_SECONDS
    except (ValueError, TypeError):
        return False


def runner_status_readonly(root: Path) -> RunnerStatusState:
    """Return runner status using only file reads and PID liveness — no fcntl.flock."""
    root = root.resolve()
    status = _read_runner_lock_metadata(root)
    if not _runner_metadata_present(status):
        return RunnerStatusState()
    if _runner_pid_is_alive(status.pid):
        if _heartbeat_is_late(status.heartbeat_at):
            return status.model_copy(update={"status": "late"})
        return status.model_copy(update={"status": "running"})
    return status.model_copy(update={"status": "stale"})


def runner_status(root: Path) -> RunnerStatusState:
    import sys

    _tasks = sys.modules["litehive.tasks"]
    root = root.resolve()
    status = _tasks._read_runner_lock_metadata(root)
    if _tasks._runner_lock_is_active(root):
        if _tasks._heartbeat_is_late(status.heartbeat_at):
            return status.model_copy(update={"status": "late"})
        return status.model_copy(update={"status": "running"})
    if not _tasks._runner_metadata_present(status):
        return RunnerStatusState()
    if _tasks._runner_status_needs_reconciliation(root):
        return status.model_copy(update={"status": "stale"})
    _tasks._clear_runner_lock_metadata(root)
    return RunnerStatusState()


def runner_status_readonly(root: Path) -> RunnerStatusState:
    """Return runner status using only file reads and PID checks — no fcntl.flock calls.

    Suitable for read-only consumers like the web dashboard that must never block
    on the runner lock.
    """
    root = root.resolve()
    status = _read_runner_lock_metadata(root)
    if not _runner_metadata_present(status):
        return RunnerStatusState()
    # If the recorded PID is alive, the runner is (or was recently) active.
    if _runner_pid_is_alive(status.pid):
        if _heartbeat_is_late(status.heartbeat_at):
            return status.model_copy(update={"status": "late"})
        return status.model_copy(update={"status": "running"})
    # PID is gone — metadata is stale.
    return status.model_copy(update={"status": "stale"})


def _touch_runner_status(
    root: Path,
    *,
    active_task_id: str | None | object = _MISSING,
) -> None:
    root = root.resolve()
    lock_state = _RUNNER_LOCKS.get(root)
    if lock_state is None:
        return
    with lock_state.metadata_lock:
        lock_state.status.heartbeat_at = utcnow()
        lock_state.status.status = "running"
        if active_task_id is not _MISSING:
            lock_state.status.active_task_id = active_task_id
        _write_runner_lock_metadata(lock_state.handle, lock_state.status)


@contextmanager
def runner_heartbeat(
    root: Path,
    *,
    active_task_id: str | None = None,
    interval_seconds: float = 1.0,
):
    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.wait(interval_seconds):
            _touch_runner_status(root, active_task_id=active_task_id)

    _touch_runner_status(root, active_task_id=active_task_id)
    thread = threading.Thread(target=_heartbeat_loop, name="litehive-runner-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=max(interval_seconds, 0.1) * 2)
        _touch_runner_status(root, active_task_id=None)


def _current_thread_owns_runner_guard(root: Path) -> bool:
    root = root.resolve()
    owner_thread_id = threading.get_ident()
    with _RUNNER_LOCKS_MUTEX:
        existing = _RUNNER_LOCKS.get(root)
    return existing is not None and existing.owner_thread_id == owner_thread_id


def _runner_pid_is_alive(pid: object) -> bool:
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
    return True


def _subagent_process_is_stale(task: "TaskRecord") -> bool:
    """Return True if the task has a recorded subagent PID that is no longer alive."""
    if task.runtime.execution_status != "running":
        return False
    active = task.runtime.active_subagent
    if active is None or active.pid is None:
        return False
    return not _runner_pid_is_alive(active.pid)


def _runner_lock_pid_is_stale(root: Path) -> bool:
    """Return True if the runner lock metadata records a PID that is no longer alive."""
    metadata = _read_runner_lock_metadata(root)
    pid = metadata.pid
    if pid is None:
        return False
    return not _runner_pid_is_alive(pid)


def _runner_lock_is_held(root: Path) -> bool:
    if _current_thread_owns_runner_guard(root):
        return True
    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def _runner_conflict_message(root: Path) -> str:
    metadata = _read_runner_lock_metadata(root)
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
    suffix = f" ({', '.join(details)})" if details else ""
    return (
        f"workspace is already being mutated by another runner{suffix}. "
        "Wait for the active run to finish before changing this workspace."
    )


@contextmanager
def workspace_runner_guard(root: Path):
    root = root.resolve()
    owner_thread_id = threading.get_ident()
    with _RUNNER_LOCKS_MUTEX:
        existing = _RUNNER_LOCKS.get(root)
        if existing is not None:
            if existing.owner_thread_id != owner_thread_id:
                raise WorkspaceConflictError(_runner_conflict_message(root))
            existing.depth += 1
    if existing is not None:
        try:
            yield
        finally:
            with _RUNNER_LOCKS_MUTEX:
                lock_state = _RUNNER_LOCKS[root]
                if lock_state.depth <= 1:
                    _RUNNER_LOCKS.pop(root, None)
                    should_close = True
                else:
                    lock_state.depth -= 1
                    should_close = False
            if should_close:
                handle = lock_state.handle
                handle.seek(0)
                handle.truncate()
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        return

    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceConflictError(_runner_conflict_message(root)) from exc
        now = utcnow()
        status = RunnerStatusState(
            status="running",
            pid=os.getpid(),
            workspace=str(root),
            command=" ".join(sys.argv),
            started_at=now,
            heartbeat_at=now,
        )
        _write_runner_lock_metadata(handle, status)
        with _RUNNER_LOCKS_MUTEX:
            _RUNNER_LOCKS[root] = _RunnerLockState(
                handle=handle, depth=1, status=status, owner_thread_id=owner_thread_id
            )
    except Exception:
        handle.close()
        raise

    try:
        yield
    finally:
        with _RUNNER_LOCKS_MUTEX:
            _RUNNER_LOCKS.pop(root, None)
        handle.seek(0)
        handle.truncate()
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def workspace_mutation_guard(root: Path):
    root = root.resolve()
    owner_thread_id = threading.get_ident()
    with _RUNNER_LOCKS_MUTEX:
        existing = _RUNNER_LOCKS.get(root)
    if existing is not None and existing.owner_thread_id == owner_thread_id:
        yield
        return
    with workspace_runner_guard(root):
        yield


def _ensure_future_task_mutation_allowed(
    root: Path,
    task_ids: list[str],
    *,
    state: WorkspaceState | None = None,
) -> None:
    from .crud import get_task
    from .queue_ops import _is_task_eligible_for_execution, active_task_markers

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
            and not _is_task_eligible_for_execution(task)
            and task.runtime.execution_status != "running"
        ):
            continue
        conflicts.append(f"{task_id} ({', '.join(markers[task_id])})")
    if conflicts:
        details = "; ".join(conflicts)
        raise WorkspaceConflictError(
            f"runner is actively using task state that cannot be changed concurrently: {details}"
        )


def _persist_future_task_update(
    root: Path,
    task: TaskRecord,
    *,
    journal_message: str | None = None,
) -> None:
    from .crud import _ensure_runtime_ignored, _serialize_task_record, _serialize_task_runtime
    from .persistence import _write_atomic_files
    from .templates import render_task_brief, task_brief_file

    task.updated_at = utcnow()
    writes = {
        task_file(root, task): _serialize_task_record(task),
        task_runtime_file(root, task): _serialize_task_runtime(task),
    }
    if journal_message is not None:
        journal_path = task_dir(root, task) / "journal.md"
        existing = journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
        writes[journal_path] = f"{existing}\n## {utcnow()}\n{journal_message}\n"
    if task.mode == "tasks":
        writes[task_brief_file(root, task)] = render_task_brief(task)
    _write_atomic_files(writes)
    _ensure_runtime_ignored(root)

