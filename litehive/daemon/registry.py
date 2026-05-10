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
from collections.abc import Iterator
from typing import Literal, TextIO

from litehive.state.lock_manager import WorkspaceLockManager
from litehive.state.locking import runner_pid_is_alive
from litehive.state.process_lock import ProcessLockManager
from litehive.state.store import RuntimeStore
from litehive.workspace import Workspace

logger = logging.getLogger(__name__)

_DAEMON_LOCKS: dict[Path, TextIO] = {}
_DAEMON_LOCKS_MUTEX = threading.Lock()


def _daemon_lock_key_impl(workspace: Workspace) -> Path:
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

    status is ``running`` when the lock is held, ``stale`` otherwise.
    pid is the daemon's OS process id, or None if missing.
    workspace is the workspace root path as a string.
    started_at is the ISO timestamp when the daemon registered.
    heartbeat_at is the most recent heartbeat timestamp (may be
        non-string for legacy rows).
    log_dir is the path to the daemon's run-all log directory.
    """

    status: Literal["running", "stale"]
    pid: int | None
    workspace: str | None
    started_at: str | None
    heartbeat_at: object
    log_dir: str | None

    @classmethod
    def from_metadata(cls, metadata: dict[str, object], status: Literal["running", "stale"]) -> "DaemonRegistryEntry":
        """
        Construct a typed entry from the raw lock-manager metadata dict.

        Extracts and coerces each field, tolerating missing or
        mistyped values from older lock-file formats.
        """
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


@dataclass(frozen=True, slots=True)
class DaemonRegistry:
    """
    Workspace-bound daemon registration API.

    Owns daemon lock metadata, process-state mirroring, and liveness checks for
    one workspace. Callers should bind a workspace once instead of passing it
    through module-level registry helpers.

    workspace is the workspace whose daemon registration this instance manages.
    """

    workspace: Workspace

    def lock_is_active(self) -> bool:
        """
        Whether a live daemon currently holds the workspace's daemon lock.
        """
        return _daemon_lock_manager_impl(self.workspace).is_active()

    def clear_stale_metadata(self, pid: int | None = None) -> None:
        """
        Drop a leftover daemon registration that no live process owns.
        """
        manager = _daemon_lock_manager_impl(self.workspace)
        manager.clear_stale_state(expected_pid=pid)

    def metadata(self) -> DaemonRegistryEntry | None:
        """
        Return the persisted daemon row tagged ``running`` or ``stale``.
        """
        manager = _daemon_lock_manager_impl(self.workspace)
        metadata = manager.read_metadata()
        if not metadata:
            return None
        if manager.is_active():
            status = "running"
        else:
            status = "stale"
        return DaemonRegistryEntry.from_metadata(metadata, status=status)

    def live_entry(self) -> DaemonRegistryEntry | None:
        """
        Return the daemon row only when a live daemon is registered.
        """
        metadata = self.metadata()
        if metadata is None:
            return None
        if metadata.status == "running":
            return metadata
        return None

    def register(self, pid: int, log_dir: Path) -> None:
        """
        Acquire the daemon lock and persist this process as the active daemon.
        """
        lock_key = _daemon_lock_key_impl(self.workspace)
        manager = _daemon_lock_manager_impl(self.workspace)
        # Remove stale lock file before opening; inherited FDs from dead
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

    def unregister(self, pid: int | None = None) -> None:
        """
        Release the daemon lock on shutdown.
        """
        lock_key = _daemon_lock_key_impl(self.workspace)
        manager = _daemon_lock_manager_impl(self.workspace)
        with _DAEMON_LOCKS_MUTEX:
            handle = _DAEMON_LOCKS.pop(lock_key, None)
        if handle is None:
            self.clear_stale_metadata(pid=pid)
            return
        try:
            metadata = manager.read_locked_metadata(handle)
            if pid is not None and metadata.get("pid") != pid:
                return
        finally:
            manager.lock_manager.release(handle, clear_metadata=True)
        manager.clear_process_state()

    def touch(self, pid: int | None = None) -> bool:
        """
        Refresh the daemon heartbeat timestamp.
        """
        lock_key = _daemon_lock_key_impl(self.workspace)
        manager = _daemon_lock_manager_impl(self.workspace)
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
    def stale_metadata(self) -> Iterator[DaemonRegistryEntry | None]:
        """
        Yield stale daemon metadata without mutating the lock file.
        """
        metadata = self.metadata()
        if metadata is None or metadata.status != "stale":
            yield None
            return
        yield metadata


def _optional_text_field(metadata: dict[str, object], key: str) -> str | None:
    """
    Extract a string value from a metadata dict, returning None for non-strings.
    """
    value = metadata.get(key)
    if isinstance(value, str):
        return value
    return None


def _daemon_lock_path_impl(workspace: Workspace) -> Path:
    """
    Canonical daemon-lockfile path for ``workspace``.

    Centralized so registry, ``daemon.execution`` status helpers, and
    recovery all read/write the same file — three modules computing
    paths independently is exactly how ".daemon.lock" and
    ".daemon-lock" diverge in practice.
    """
    return workspace.runtime_path("runtime", ".daemon.lock")


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


def _daemon_lock_manager_impl(workspace: Workspace) -> ProcessLockManager:
    """
    Build a per-call ``ProcessLockManager`` bound to this workspace.

    Cheap to construct — keeps the registry stateless from one call
    to the next and avoids a long-lived global handle that would
    leak across workspace switches in the test suite. The lock
    manager itself is the shared primitive; this helper only wires
    up the daemon-specific liveness predicate.
    """
    lock_key = _daemon_lock_key_impl(workspace)
    return ProcessLockManager(
        process_name="daemon",
        lock_manager=WorkspaceLockManager(
            path=_daemon_lock_path_impl(workspace),
            pid_is_alive=runner_pid_is_alive,
            held_in_process=lambda: _daemon_lock_is_held_in_process(lock_key),
            fsync_writes=True,
        ),
        runtime_store=RuntimeStore(workspace),
    )
