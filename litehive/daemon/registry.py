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
from dataclasses import dataclass
import logging
from pathlib import Path
import threading
from typing import Literal, TextIO

from litehive.config.paths import workspace_path
from litehive.state.lock_manager import WorkspaceLockManager
from litehive.state.locking import runner_pid_is_alive
from litehive.state.process_lock import ProcessLockManager
from litehive.state.store import runtime_store_for_workspace
from litehive.workspace import Workspace

logger = logging.getLogger(__name__)

_DAEMON_LOCKS: dict[Path, TextIO] = {}
_DAEMON_LOCKS_MUTEX = threading.Lock()


def _daemon_lock_key_for_workspace(workspace: Workspace) -> Path:
    """
    Return the normalized key for the in-process daemon-lock registry.
    """
    return workspace.root.resolve()


@dataclass(frozen=True, slots=True)
class DaemonRegistryEntry:
    """
    Typed daemon registration row exposed to daemon/status consumers.

    The lock manager still persists JSON-shaped metadata because that
    is the on-disk format. The registry converts that boundary shape
    once so callers do not keep reaching into ``dict[str, object]``
    for pid, status, heartbeat, and log path fields.
    """

    status: Literal["running", "stale"]
    pid: int | None
    workspace: str | None
    started_at: str | None
    heartbeat_at: object
    log_dir: str | None

    @classmethod
    def from_metadata(cls, metadata: dict[str, object], status: Literal["running", "stale"]) -> "DaemonRegistryEntry":
        pid_value = metadata.get("pid")
        if isinstance(pid_value, int):
            pid = pid_value
        else:
            pid = None
        workspace = _optional_text_field(metadata, "workspace")
        started_at = _optional_text_field(metadata, "started_at")
        log_dir = _optional_text_field(metadata, "log_dir")
        return cls(
            status=status,
            pid=pid,
            workspace=workspace,
            started_at=started_at,
            heartbeat_at=metadata.get("heartbeat_at"),
            log_dir=log_dir,
        )


def _optional_text_field(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if isinstance(value, str):
        return value
    return None


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
    Path-based compatibility wrapper for daemon lock manager construction.
    """
    return _daemon_lock_manager_for_workspace(Workspace.from_path(workspace))


def _daemon_lock_manager_for_workspace(workspace: Workspace) -> ProcessLockManager:
    """
    Build a per-call ``ProcessLockManager`` bound to this workspace.

    Cheap to construct — keeps the registry stateless from one call
    to the next and avoids a long-lived global handle that would
    leak across workspace switches in the test suite. The lock
    manager itself is the shared primitive; this helper only wires
    up the daemon-specific liveness predicate.
    """
    lock_key = _daemon_lock_key_for_workspace(workspace)
    return ProcessLockManager(
        process_name="daemon",
        lock_manager=WorkspaceLockManager(
            path=daemon_lock_path(lock_key),
            pid_is_alive=runner_pid_is_alive,
            held_in_process=lambda: _daemon_lock_is_held_in_process(lock_key),
            fsync_writes=True,
        ),
        runtime_store=runtime_store_for_workspace(workspace),
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
    Path-based compatibility wrapper for stale daemon metadata cleanup.
    """
    _clear_stale_daemon_metadata_for_workspace(Workspace.from_path(workspace), pid=pid)


def _clear_stale_daemon_metadata_for_workspace(workspace: Workspace, pid: int | None = None) -> None:
    """
    Drop a leftover daemon registration that no live process owns.

    Reached by ``unregister_daemon`` and the start-background reclaim
    path, so a dead daemon's row never blocks a new registration.
    The optional ``pid`` guard prevents this from clearing a fresh
    daemon's row when an older daemon's stop sequence raced an
    immediate restart.
    """
    manager = _daemon_lock_manager_for_workspace(workspace)
    manager.clear_stale_state(expected_pid=pid)


def daemon_metadata(workspace: Path) -> DaemonRegistryEntry | None:
    """
    Path-based compatibility wrapper for daemon metadata reads.
    """
    return daemon_metadata_for_workspace(Workspace.from_path(workspace))


def daemon_metadata_for_workspace(workspace: Workspace) -> DaemonRegistryEntry | None:
    """
    Return the persisted daemon row tagged ``running`` or ``stale``.

    Status and health surfaces consume this when they want both
    "what was registered" and "is it still alive" in one call —
    distinguishing those two states is the whole point of having a
    registry, since a dead-but-recorded daemon is not the same as
    no daemon at all.
    """
    manager = _daemon_lock_manager_for_workspace(workspace)
    metadata = manager.read_metadata()
    if not metadata:
        return None
    if manager.is_active():
        status = "running"
    else:
        status = "stale"
    return DaemonRegistryEntry.from_metadata(metadata, status=status)


def get_workspace_daemon(workspace: Path) -> DaemonRegistryEntry | None:
    """
    Path-based compatibility wrapper for live daemon lookup.
    """
    return get_workspace_daemon_for_workspace(Workspace.from_path(workspace))


def get_workspace_daemon_for_workspace(workspace: Workspace) -> DaemonRegistryEntry | None:
    """
    Return the daemon row only when a live daemon is registered.

    The strict counterpart to ``daemon_metadata``: callers that would
    act on the row (signal it, deduplicate against it, refuse to
    start) want to skip stale entries automatically rather than
    branching on ``status`` themselves and risk acting on a corpse.
    """
    metadata = daemon_metadata_for_workspace(workspace)
    if metadata is None:
        return None
    if metadata.status == "running":
        return metadata
    return None


def register_daemon(workspace: Path, pid: int, log_dir: Path) -> None:
    """
    Path-based compatibility wrapper for daemon registration.
    """
    register_daemon_for_workspace(Workspace.from_path(workspace), pid=pid, log_dir=log_dir)


def register_daemon_for_workspace(workspace: Workspace, pid: int, log_dir: Path) -> None:
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
    lock_key = _daemon_lock_key_for_workspace(workspace)
    manager = _daemon_lock_manager_for_workspace(workspace)
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
                raise RuntimeError(f"daemon already running for {lock_key}: pid={existing_pid}") from None
            raise RuntimeError(f"daemon already running for {lock_key}: pid={existing_pid}") from None
        payload = manager.create_base_metadata(
            pid,
            {
                "workspace": str(lock_key),
                "log_dir": str(log_dir),
            },
        )
        manager.write_locked_metadata(handle, payload)
        with _DAEMON_LOCKS_MUTEX:
            existing_handle = _DAEMON_LOCKS.get(lock_key)
            if existing_handle is not None:
                raise RuntimeError(f"daemon already registered in-process for {lock_key}")
            _DAEMON_LOCKS[lock_key] = handle
        manager.save_process_state(payload)
    except (OSError, RuntimeError):
        try:
            manager.lock_manager.unlock(handle)
        except OSError:
            pass
        handle.close()
        raise


def unregister_daemon(workspace: Path, pid: int | None = None) -> None:
    """
    Path-based compatibility wrapper for daemon unregistration.
    """
    unregister_daemon_for_workspace(Workspace.from_path(workspace), pid=pid)


def unregister_daemon_for_workspace(workspace: Workspace, pid: int | None = None) -> None:
    """
    Release the daemon lock on shutdown.

    Called from ``run_daemon_loop``'s ``finally`` and from the stop
    path. The ``pid`` guard prevents an old daemon's tear-down from
    clearing a fresh daemon's row when the two raced on the same
    workspace path; without it, a slow ``unregister_daemon`` could
    silently delete an unrelated daemon's registration.
    """
    lock_key = _daemon_lock_key_for_workspace(workspace)
    manager = _daemon_lock_manager_for_workspace(workspace)
    with _DAEMON_LOCKS_MUTEX:
        handle = _DAEMON_LOCKS.pop(lock_key, None)
    if handle is None:
        _clear_stale_daemon_metadata_for_workspace(workspace, pid=pid)
        return
    try:
        metadata = manager.read_locked_metadata(handle)
        if pid is not None and metadata.get("pid") != pid:
            return
    finally:
        manager.lock_manager.release(handle, clear_metadata=True)
    manager.clear_process_state()


def touch_daemon(workspace: Path, pid: int | None = None) -> bool:
    """
    Path-based compatibility wrapper for daemon heartbeat updates.
    """
    return touch_daemon_for_workspace(Workspace.from_path(workspace), pid=pid)


def touch_daemon_for_workspace(workspace: Workspace, pid: int | None = None) -> bool:
    """
    Refresh the daemon heartbeat timestamp.

    External observers (status, the daemon-start guard) read the
    heartbeat to tell a live daemon from a wedged one. Called by the
    background heartbeat thread on the configured daemon heartbeat
    interval so a long iteration of the main loop never lets the
    daemon look dead. Returns ``False`` when the in-process row has
    gone (so the heartbeat thread can stop trying after shutdown
    began).
    """
    lock_key = _daemon_lock_key_for_workspace(workspace)
    manager = _daemon_lock_manager_for_workspace(workspace)
    with _DAEMON_LOCKS_MUTEX:
        handle = _DAEMON_LOCKS.get(lock_key)
        if handle is None:
            return False
        metadata = manager.read_locked_metadata(handle)
        if pid is not None and metadata.get("pid") != pid:
            return False
        manager.update_heartbeat(handle)
        metadata = manager.read_locked_metadata(handle)
        manager.save_process_state(metadata)
    return True


@contextmanager
def stale_daemon_metadata(workspace: Path):
    """
    Path-based compatibility wrapper for stale daemon metadata reads.
    """
    with stale_daemon_metadata_for_workspace(Workspace.from_path(workspace)) as metadata:
        yield metadata


@contextmanager
def stale_daemon_metadata_for_workspace(workspace: Workspace):
    """
    Yield stale daemon metadata without mutating the lock file.

    Used by recovery and operator-facing diagnostics that want to
    surface a dead daemon's last known facts (pid, log_dir,
    started_at) without taking responsibility for clearing the row.
    Yields ``None`` when the registration is healthy or absent so
    callers can keep their handler symmetric.
    """
    metadata = daemon_metadata_for_workspace(workspace)
    if metadata is None or metadata.status != "stale":
        yield None
        return
    yield metadata
