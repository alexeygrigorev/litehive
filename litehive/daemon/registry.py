"""Workspace-local daemon metadata backed by a lock file."""

from contextlib import contextmanager
import fcntl
import logging
import os
from pathlib import Path
import threading
from typing import TextIO

import yaml

from litehive.config.paths import litehive_root, workspace_path
from litehive.config.registry import list_registered_workspace_paths
from litehive.domain.common import utcnow
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


@contextmanager
def _locked_daemon_registry() -> TextIO:
    lock_path = _daemon_registry_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    _remove_daemon_registry_entry(workspace)


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
    # Remove stale lock file before opening — inherited FDs from dead
    # processes can hold flocks on the old inode indefinitely.
    if path.exists():
        existing = _read_metadata(path) or {}
        existing_pid = existing.get("pid")
        if isinstance(existing_pid, int) and not pid_is_alive(existing_pid):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            existing = _read_locked_metadata(handle)
            existing_pid = existing.get("pid")
            if isinstance(existing_pid, int) and pid_is_alive(existing_pid):
                raise RuntimeError(f"daemon already running for {workspace}: pid={existing_pid}") from None
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
        _upsert_daemon_registry_entry(workspace, payload)
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
        _remove_daemon_registry_entry(workspace)
        return
    _clear_stale_daemon_metadata(workspace, pid=pid)


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
