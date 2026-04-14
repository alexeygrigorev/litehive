"""Workspace-local daemon metadata backed by a lock file."""

from contextlib import contextmanager
import fcntl
import logging
import os
from pathlib import Path
import threading
from typing import TextIO

import yaml

from litehive.config.paths import workspace_daemon_lock_path
from litehive.config.registry import list_registered_workspace_paths
from litehive.domain.common import utcnow
from litehive.state.locking import runner_pid_is_alive as pid_is_alive

logger = logging.getLogger(__name__)

_DAEMON_LOCKS: dict[Path, TextIO] = {}
_DAEMON_LOCKS_MUTEX = threading.Lock()
def daemon_lock_path(workspace: Path) -> Path:
    return workspace_daemon_lock_path(workspace.resolve())


def _read_metadata(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    return dict(data)


def _read_locked_metadata(handle: TextIO) -> dict[str, object]:
    handle.seek(0)
    data = yaml.safe_load(handle.read()) or {}
    if not isinstance(data, dict):
        return {}
    return dict(data)


def _write_locked_metadata(handle: TextIO, payload: dict[str, object]) -> None:
    handle.seek(0)
    handle.truncate()
    yaml.safe_dump(payload, handle, sort_keys=False)
    handle.flush()
    os.fsync(handle.fileno())


def daemon_lock_is_active(workspace: Path) -> bool:
    workspace = workspace.resolve()
    with _DAEMON_LOCKS_MUTEX:
        if workspace in _DAEMON_LOCKS:
            return True
    path = daemon_lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def _clear_stale_daemon_metadata(workspace: Path, *, pid: int | None = None) -> None:
    workspace = workspace.resolve()
    path = daemon_lock_path(workspace)
    if not path.exists():
        return
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        metadata = _read_locked_metadata(handle)
        metadata_pid = metadata.get("pid")
        if pid is not None and metadata_pid != pid:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        if isinstance(metadata_pid, int) and pid_is_alive(metadata_pid):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        handle.seek(0)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def daemon_metadata(workspace: Path) -> dict[str, object] | None:
    workspace = workspace.resolve()
    metadata = _read_metadata(daemon_lock_path(workspace))
    if not metadata:
        return None
    pid = metadata.get("pid")
    if daemon_lock_is_active(workspace):
        payload = dict(metadata)
        payload["status"] = "running"
        return payload
    if isinstance(pid, int) and pid_is_alive(pid):
        payload = dict(metadata)
        payload["status"] = "running"
        return payload
    payload = dict(metadata)
    payload["status"] = "stale"
    return payload


def get_workspace_daemon(workspace: Path) -> dict[str, object] | None:
    metadata = daemon_metadata(workspace)
    if metadata is None:
        return None
    return metadata if metadata.get("status") == "running" else None


def register_daemon(workspace: Path, *, pid: int, log_dir: Path) -> None:
    workspace = workspace.resolve()
    path = daemon_lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            existing = _read_locked_metadata(handle)
            existing_pid = existing.get("pid")
            raise RuntimeError(f"daemon already running for {workspace}: pid={existing_pid}") from None
        payload = {
            "workspace": str(workspace),
            "pid": pid,
            "started_at": utcnow(),
            "log_dir": str(log_dir),
        }
        _write_locked_metadata(handle, payload)
        with _DAEMON_LOCKS_MUTEX:
            existing_handle = _DAEMON_LOCKS.get(workspace)
            if existing_handle is not None:
                raise RuntimeError(f"daemon already registered in-process for {workspace}")
            _DAEMON_LOCKS[workspace] = handle
    except Exception:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()
        raise


def unregister_daemon(workspace: Path, *, pid: int | None = None) -> None:
    workspace = workspace.resolve()
    with _DAEMON_LOCKS_MUTEX:
        handle = _DAEMON_LOCKS.pop(workspace, None)
    if handle is not None:
        try:
            metadata = _read_locked_metadata(handle)
            if pid is not None and metadata.get("pid") != pid:
                return
            handle.seek(0)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        return
    _clear_stale_daemon_metadata(workspace, pid=pid)


def list_daemon_instances() -> list[dict[str, object]]:
    instances: list[dict[str, object]] = []
    for workspace in list_registered_workspace_paths():
        metadata = daemon_metadata(workspace)
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
