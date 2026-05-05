"""Workspace-local daemon metadata backed by a lock file."""

from contextlib import contextmanager
import logging
from pathlib import Path
import threading
from typing import TextIO

from litehive.config.paths import workspace_path
from litehive.state.process_lock import ProcessLockManager
from litehive.state.locking import runner_pid_is_alive as pid_is_alive

logger = logging.getLogger(__name__)

_DAEMON_LOCKS: dict[Path, TextIO] = {}
_DAEMON_LOCKS_MUTEX = threading.Lock()


def daemon_lock_path(workspace: Path) -> Path:
    return workspace_path(workspace.resolve(), "runtime", ".daemon.lock")


def _daemon_lock_is_held_in_process(workspace: Path) -> bool:
    with _DAEMON_LOCKS_MUTEX:
        return workspace in _DAEMON_LOCKS


def _daemon_lock_manager(workspace: Path) -> ProcessLockManager:
    workspace = workspace.resolve()
    return ProcessLockManager(
        lock_path=daemon_lock_path(workspace),
        process_name="daemon",
        pid_is_alive=pid_is_alive,
        held_in_process=lambda: _daemon_lock_is_held_in_process(workspace),
        fsync_writes=True,
    )


def daemon_lock_is_active(workspace: Path) -> bool:
    workspace = workspace.resolve()
    return _daemon_lock_manager(workspace).is_active()


def _clear_stale_daemon_metadata(workspace: Path, pid: int | None = None) -> None:
    workspace = workspace.resolve()
    manager = _daemon_lock_manager(workspace)
    manager.clear_stale_state(workspace, expected_pid=pid)


def daemon_metadata(workspace: Path) -> dict[str, object] | None:
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
    metadata = daemon_metadata(workspace)
    if metadata is None:
        return None
    if metadata.get("status") == "running":
        return metadata
    return None


def register_daemon(workspace: Path, pid: int, log_dir: Path) -> None:
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
            if isinstance(existing_pid, int) and pid_is_alive(existing_pid):
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
    workspace = workspace.resolve()
    manager = _daemon_lock_manager(workspace)
    with _DAEMON_LOCKS_MUTEX:
        handle = _DAEMON_LOCKS.pop(workspace, None)
    if handle is not None:
        try:
            metadata = manager.read_locked_metadata(handle)
            if pid is not None and metadata.get("pid") != pid:
                return
        finally:
            manager.lock_manager.release(handle, clear_metadata=True)
        manager.clear_process_state(workspace)
        return
    _clear_stale_daemon_metadata(workspace, pid=pid)


def touch_daemon(workspace: Path, pid: int | None = None) -> bool:
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
    """Yield stale daemon metadata without mutating the lock file."""
    metadata = daemon_metadata(workspace)
    if metadata is None or metadata.get("status") != "stale":
        yield None
        return
    yield metadata
