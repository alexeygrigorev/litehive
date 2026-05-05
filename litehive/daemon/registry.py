"""
Workspace-local daemon registration backed by an flock'd metadata file.

The registry answers "is there a live daemon for this workspace?" and
"who is it?" without an extra heartbeat protocol. Each daemon takes
out an exclusive flock on ``$workspace/runtime/.daemon.lock`` and
writes its pid + start_at + log_dir into the same file; readers (CLI
status, daemon start guard, recovery) read the file and check pid
liveness to distinguish a running daemon from a leftover row.
``ProcessLockManager`` is the shared lock-file primitive — this
module is the daemon-specific consumer.
"""

from contextlib import contextmanager
import logging
from pathlib import Path
import threading
from typing import TextIO

from litehive.config.paths import workspace_path
from litehive.state.process_lock import ProcessLockManager
from litehive.state.locking import runner_pid_is_alive

logger = logging.getLogger(__name__)

_DAEMON_LOCKS: dict[Path, TextIO] = {}
_DAEMON_LOCKS_MUTEX = threading.Lock()


def daemon_lock_path(workspace: Path) -> Path:
    """
    Canonical daemon-lockfile path for ``workspace``.

    Centralized so registry, ``daemon.execution`` status helpers, and
    recovery all read/write the same file — three modules computing
    paths independently is exactly how ".daemon.lock" and
    ".daemon-lock" diverge in practice.
    """
    return workspace_path(workspace.resolve(), "runtime", ".daemon.lock")


def _daemon_lock_is_held_in_process(workspace: Path) -> bool:
    """
    True when this Python process already holds the daemon lock for ``workspace``.

    Lets ``ProcessLockManager.is_active`` distinguish "this process
    owns the lock" from "someone else does" without flock'ing
    against itself (advisory locks are reentrant in some kernels and
    not others, so we avoid relying on that behavior). Reads the
    in-process bookkeeping under ``_DAEMON_LOCKS_MUTEX``.
    """
    with _DAEMON_LOCKS_MUTEX:
        return workspace in _DAEMON_LOCKS


def _daemon_lock_manager(workspace: Path) -> ProcessLockManager:
    """
    Build a per-call ``ProcessLockManager`` bound to this workspace.

    Cheap to construct — keeps the registry stateless from one call
    to the next and avoids a long-lived global handle that would
    leak across workspace switches in the test suite. The lock
    manager itself is the shared primitive; this helper only wires
    up the daemon-specific liveness predicate.
    """
    workspace = workspace.resolve()
    return ProcessLockManager(
        lock_path=daemon_lock_path(workspace),
        process_name="daemon",
        pid_is_alive=runner_pid_is_alive,
        held_in_process=lambda: _daemon_lock_is_held_in_process(workspace),
        fsync_writes=True,
    )


def daemon_lock_is_active(workspace: Path) -> bool:
    """
    Whether a live daemon currently holds the workspace's daemon lock.

    Used by the CLI status path and the start-guard check in
    ``daemon.execution``. Differs from the raw lockfile-exists check
    by also confirming the recorded pid is alive — a leftover lock
    file from a crashed daemon is not an active daemon.
    """
    workspace = workspace.resolve()
    return _daemon_lock_manager(workspace).is_active()


def _clear_stale_daemon_metadata(workspace: Path, pid: int | None = None) -> None:
    """
    Drop a leftover daemon registration that no live process owns.

    Reached by ``unregister_daemon`` and the start-background reclaim
    path, so a dead daemon's row never blocks a new registration.
    The optional ``pid`` guard prevents this from clearing a fresh
    daemon's row when an older daemon's stop sequence raced an
    immediate restart.
    """
    workspace = workspace.resolve()
    manager = _daemon_lock_manager(workspace)
    manager.clear_stale_state(workspace, expected_pid=pid)


def daemon_metadata(workspace: Path) -> dict[str, object] | None:
    """
    Return the persisted daemon row tagged ``running`` or ``stale``.

    Status and health surfaces consume this when they want both
    "what was registered" and "is it still alive" in one call —
    distinguishing those two states is the whole point of having a
    registry, since a dead-but-recorded daemon is not the same as
    no daemon at all.
    """
    workspace = workspace.resolve()
    manager = _daemon_lock_manager(workspace)
    metadata = manager.read_metadata()
    if not metadata:
        return None
    if manager.is_active():
        status = "running"
    else:
        status = "stale"
    payload = dict(metadata)
    payload["status"] = status
    return payload


def get_workspace_daemon(workspace: Path) -> dict[str, object] | None:
    """
    Return the daemon row only when a live daemon is registered.

    The strict counterpart to ``daemon_metadata``: callers that would
    act on the row (signal it, deduplicate against it, refuse to
    start) want to skip stale entries automatically rather than
    branching on ``status`` themselves and risk acting on a corpse.
    """
    metadata = daemon_metadata(workspace)
    if metadata is None:
        return None
    if metadata.get("status") == "running":
        return metadata
    return None


def register_daemon(workspace: Path, pid: int, log_dir: Path) -> None:
    """
    Acquire the daemon lock and persist this process as the active daemon.

    Called once at the top of ``run_daemon_loop`` so the registration
    row is in place before the first heartbeat. Raises if another
    live daemon already owns the lock — two concurrent daemons for
    one workspace would corrupt task state. The pre-open
    ``remove_stale_lockfile`` step is load-bearing on Linux where an
    inherited fd from a dead process can keep the flock alive on the
    old inode.
    """
    workspace = workspace.resolve()
    manager = _daemon_lock_manager(workspace)
    # Remove stale lock file before opening — inherited FDs from dead
    # processes can hold flocks on the old inode indefinitely.
    manager.remove_stale_lockfile()
    handle = manager.lock_manager.open()
    try:
        try:
            manager.lock_manager.lock(handle, nonblocking=True)
        except BlockingIOError:
            existing = manager.read_locked_metadata(handle)
            existing_pid = existing.get("pid")
            if isinstance(existing_pid, int) and runner_pid_is_alive(existing_pid):
                raise RuntimeError(f"daemon already running for {workspace}: pid={existing_pid}") from None
            raise RuntimeError(f"daemon already running for {workspace}: pid={existing_pid}") from None
        payload = manager.create_base_metadata(
            pid,
            {
                "workspace": str(workspace),
                "log_dir": str(log_dir),
            },
        )
        manager.write_locked_metadata(handle, payload)
        with _DAEMON_LOCKS_MUTEX:
            existing_handle = _DAEMON_LOCKS.get(workspace)
            if existing_handle is not None:
                raise RuntimeError(f"daemon already registered in-process for {workspace}")
            _DAEMON_LOCKS[workspace] = handle
        manager.save_process_state(workspace, payload)
    except (OSError, RuntimeError):
        try:
            manager.lock_manager.unlock(handle)
        except OSError:
            pass
        handle.close()
        raise


def unregister_daemon(workspace: Path, pid: int | None = None) -> None:
    """
    Release the daemon lock on shutdown.

    Called from ``run_daemon_loop``'s ``finally`` and from the stop
    path. The ``pid`` guard prevents an old daemon's tear-down from
    clearing a fresh daemon's row when the two raced on the same
    workspace path; without it, a slow ``unregister_daemon`` could
    silently delete an unrelated daemon's registration.
    """
    workspace = workspace.resolve()
    manager = _daemon_lock_manager(workspace)
    with _DAEMON_LOCKS_MUTEX:
        handle = _DAEMON_LOCKS.pop(workspace, None)
    if handle is None:
        _clear_stale_daemon_metadata(workspace, pid=pid)
        return
    try:
        metadata = manager.read_locked_metadata(handle)
        if pid is not None and metadata.get("pid") != pid:
            return
    finally:
        manager.lock_manager.release(handle, clear_metadata=True)
    manager.clear_process_state(workspace)


def touch_daemon(workspace: Path, pid: int | None = None) -> bool:
    """
    Refresh the daemon heartbeat timestamp.

    External observers (status, the daemon-start guard) read the
    heartbeat to tell a live daemon from a wedged one. Called by the
    background heartbeat thread once per
    ``_DAEMON_HEARTBEAT_INTERVAL_SECONDS`` so a long iteration of
    the main loop never lets the daemon look dead. Returns ``False``
    when the in-process row has gone (so the heartbeat thread can
    stop trying after shutdown began).
    """
    workspace = workspace.resolve()
    manager = _daemon_lock_manager(workspace)
    with _DAEMON_LOCKS_MUTEX:
        handle = _DAEMON_LOCKS.get(workspace)
        if handle is None:
            return False
        metadata = manager.read_locked_metadata(handle)
        if pid is not None and metadata.get("pid") != pid:
            return False
        manager.update_heartbeat(handle)
        metadata = manager.read_locked_metadata(handle)
        manager.save_process_state(workspace, metadata)
    return True


@contextmanager
def stale_daemon_metadata(workspace: Path):
    """
    Yield stale daemon metadata without mutating the lock file.

    Used by recovery and operator-facing diagnostics that want to
    surface a dead daemon's last known facts (pid, log_dir,
    started_at) without taking responsibility for clearing the row.
    Yields ``None`` when the registration is healthy or absent so
    callers can keep their handler symmetric.
    """
    metadata = daemon_metadata(workspace)
    if metadata is None or metadata.get("status") != "stale":
        yield None
        return
    yield metadata
