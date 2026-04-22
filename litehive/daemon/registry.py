"""Workspace-local daemon metadata backed by a lock file."""

from contextlib import contextmanager
import logging
from pathlib import Path
import threading
from typing import TextIO

import yaml

from litehive.config.paths import litehive_root, workspace_path
from litehive.config.registry import list_registered_workspace_paths
from litehive.domain.common import utcnow
from litehive.state.lock_manager import WorkspaceLockManager
from litehive.state.locking import runner_pid_is_alive as pid_is_alive

logger = logging.getLogger(__name__)

_DAEMON_LOCKS: dict[Path, TextIO] = {}
_DAEMON_LOCKS_MUTEX = threading.Lock()
_DAEMON_REGISTRY_MUTEX = threading.Lock()


def daemon_lock_path(workspace: Path) -> Path:
    return workspace_path(workspace.resolve(), "runtime", ".daemon.lock")


def _daemon_registry_path() -> Path:
    return litehive_root() / "daemons.yaml"


def _daemon_registry_lock_path() -> Path:
    return litehive_root() / ".daemons.lock"


def _daemon_lock_is_held_in_process(workspace: Path) -> bool:
    with _DAEMON_LOCKS_MUTEX:
        return workspace in _DAEMON_LOCKS


def _daemon_lock_manager(workspace: Path) -> WorkspaceLockManager:
    workspace = workspace.resolve()
    return WorkspaceLockManager(
        daemon_lock_path(workspace),
        pid_is_alive=pid_is_alive,
        held_in_process=lambda: _daemon_lock_is_held_in_process(workspace),
        fsync_writes=True,
    )


def _read_metadata(path: Path) -> dict[str, object] | None:
    return WorkspaceLockManager(path, pid_is_alive=pid_is_alive, fsync_writes=True).read_metadata()


def _read_locked_metadata(handle: TextIO) -> dict[str, object]:
    return WorkspaceLockManager(Path(handle.name), pid_is_alive=pid_is_alive, fsync_writes=True).read_locked_metadata(handle)


def _write_locked_metadata(handle: TextIO, payload: dict[str, object]) -> None:
    WorkspaceLockManager(Path(handle.name), pid_is_alive=pid_is_alive, fsync_writes=True).write_locked_metadata(handle, payload)


@contextmanager
def _locked_daemon_registry() -> TextIO:
    lock_path = _daemon_registry_lock_path()
    manager = WorkspaceLockManager(lock_path, pid_is_alive=pid_is_alive, fsync_writes=True)
    with manager.open() as handle:
        manager.lock(handle, nonblocking=False)
        try:
            yield handle
        finally:
            manager.unlock(handle)


def _read_daemon_registry() -> list[dict[str, object]]:
    path = _daemon_registry_path()
    if not path.exists():
        return []
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(payload, list):
        return []
    return [dict(entry) for entry in payload if isinstance(entry, dict)]


def _write_daemon_registry(entries: list[dict[str, object]]) -> None:
    path = _daemon_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")


def _upsert_daemon_registry_entry(workspace: Path, payload: dict[str, object]) -> None:
    with _DAEMON_REGISTRY_MUTEX:
        with _locked_daemon_registry():
            entries = [entry for entry in _read_daemon_registry() if entry.get("workspace") != str(workspace)]
            entries.append(payload)
            _write_daemon_registry(sorted(entries, key=lambda entry: str(entry.get("workspace", ""))))


def _remove_daemon_registry_entry(workspace: Path) -> None:
    with _DAEMON_REGISTRY_MUTEX:
        with _locked_daemon_registry():
            entries = [entry for entry in _read_daemon_registry() if entry.get("workspace") != str(workspace)]
            _write_daemon_registry(entries)


def daemon_lock_is_active(workspace: Path) -> bool:
    workspace = workspace.resolve()
    return _daemon_lock_manager(workspace).is_active()


def _clear_stale_daemon_metadata(workspace: Path, *, pid: int | None = None) -> None:
    workspace = workspace.resolve()
    if _daemon_lock_manager(workspace).clear_metadata_if_unlocked(expected_pid=pid, require_stale_pid=True):
        _remove_daemon_registry_entry(workspace)


def daemon_metadata(workspace: Path) -> dict[str, object] | None:
    workspace = workspace.resolve()
    manager = _daemon_lock_manager(workspace)
    metadata = manager.read_metadata()
    if not metadata:
        return None
    status = "running" if manager.is_active() else "stale"
    payload = dict(metadata)
    payload["status"] = status
    return payload


def get_workspace_daemon(workspace: Path) -> dict[str, object] | None:
    metadata = daemon_metadata(workspace)
    if metadata is None:
        return None
    return metadata if metadata.get("status") == "running" else None


def register_daemon(workspace: Path, *, pid: int, log_dir: Path) -> None:
    workspace = workspace.resolve()
    manager = _daemon_lock_manager(workspace)
    # Remove stale lock file before opening — inherited FDs from dead
    # processes can hold flocks on the old inode indefinitely.
    manager.remove_stale_lockfile()
    handle = manager.open()
    try:
        try:
            manager.lock(handle, nonblocking=True)
        except BlockingIOError:
            existing = manager.read_locked_metadata(handle)
            existing_pid = existing.get("pid")
            if isinstance(existing_pid, int) and pid_is_alive(existing_pid):
                raise RuntimeError(f"daemon already running for {workspace}: pid={existing_pid}") from None
            raise RuntimeError(f"daemon already running for {workspace}: pid={existing_pid}") from None
        now = utcnow()
        payload = {
            "workspace": str(workspace),
            "pid": pid,
            "started_at": now,
            "heartbeat_at": now,
            "log_dir": str(log_dir),
        }
        manager.write_locked_metadata(handle, payload)
        with _DAEMON_LOCKS_MUTEX:
            existing_handle = _DAEMON_LOCKS.get(workspace)
            if existing_handle is not None:
                raise RuntimeError(f"daemon already registered in-process for {workspace}")
            _DAEMON_LOCKS[workspace] = handle
        _upsert_daemon_registry_entry(workspace, payload)
    except Exception:
        try:
            manager.unlock(handle)
        except OSError:
            pass
        handle.close()
        raise


def unregister_daemon(workspace: Path, *, pid: int | None = None) -> None:
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
            manager.release(handle, clear_metadata=True)
        _remove_daemon_registry_entry(workspace)
        return
    _clear_stale_daemon_metadata(workspace, pid=pid)


def touch_daemon(workspace: Path, *, pid: int | None = None) -> bool:
    workspace = workspace.resolve()
    manager = _daemon_lock_manager(workspace)
    with _DAEMON_LOCKS_MUTEX:
        handle = _DAEMON_LOCKS.get(workspace)
        if handle is None:
            return False
        metadata = manager.read_locked_metadata(handle)
        if pid is not None and metadata.get("pid") != pid:
            return False
        metadata["heartbeat_at"] = utcnow()
        manager.write_locked_metadata(handle, metadata)
    return True


def list_daemon_instances() -> list[dict[str, object]]:
    instances: list[dict[str, object]] = []
    daemon_workspaces: list[Path] = []
    for entry in _read_daemon_registry():
        workspace = entry.get("workspace")
        if isinstance(workspace, str):
            daemon_workspaces.append(Path(workspace))
    if not daemon_workspaces:
        daemon_workspaces = list_registered_workspace_paths()
    for workspace in daemon_workspaces:
        metadata = daemon_metadata(workspace.resolve())
        if metadata is None or metadata.get("status") != "running":
            continue
        instances.append(metadata)
    return sorted(instances, key=lambda item: str(item.get("workspace", "")))


@contextmanager
def stale_daemon_metadata(workspace: Path):
    """Yield stale daemon metadata without mutating the lock file."""
    metadata = daemon_metadata(workspace)
    if metadata is None or metadata.get("status") != "stale":
        yield None
        return
    yield metadata
