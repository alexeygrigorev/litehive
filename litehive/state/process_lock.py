"""Shared process lock management for runners and daemons."""

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from litehive.domain.common import utcnow
from litehive.state.lock_manager import WorkspaceLockManager


@dataclass
class ProcessLockManager:
    """High-level process lock manager for runner and daemon processes."""

    lock_path: Path
    process_name: str
    pid_is_alive: Callable[[object], bool]
    held_in_process: Callable[[], bool] | None = None
    fsync_writes: bool = False
    lock_manager: WorkspaceLockManager = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.lock_manager = WorkspaceLockManager(
            path=self.lock_path,
            pid_is_alive=self.pid_is_alive,
            held_in_process=self.held_in_process,
            fsync_writes=self.fsync_writes,
        )

    def is_active(self) -> bool:
        """Check if the lock is currently active."""
        return self.lock_manager.is_active()

    def read_metadata(self, *, strict: bool = False) -> dict[str, object] | None:
        """Read lock metadata from file."""
        return self.lock_manager.read_metadata(strict=strict)

    def read_locked_metadata(self, handle: TextIO) -> dict[str, object]:
        """Read metadata from an already-locked file handle."""
        return self.lock_manager.read_locked_metadata(handle)

    def write_locked_metadata(self, handle: TextIO, payload: Mapping[str, object]) -> None:
        """Write metadata to an already-locked file handle."""
        self.lock_manager.write_locked_metadata(handle, payload)

    def clear_metadata_if_unlocked(
        self,
        *,
        expected_pid: int | None = None,
        require_stale_pid: bool = False,
    ) -> bool:
        """Clear metadata if the lock is unlocked and conditions are met."""
        return self.lock_manager.clear_metadata_if_unlocked(
            expected_pid=expected_pid,
            require_stale_pid=require_stale_pid,
        )

    def pid_is_stale(self) -> bool:
        """Check if the recorded PID is stale (process no longer alive)."""
        return self.lock_manager.pid_is_stale()

    def remove_stale_lockfile(self) -> bool:
        """Remove lock file if it contains a stale PID."""
        return self.lock_manager.remove_stale_lockfile()

    @contextmanager
    def open_locked(self, *, nonblocking: bool = True):
        """Context manager for acquiring and releasing a lock."""
        handle = self.lock_manager.acquire(nonblocking=nonblocking)
        try:
            yield handle
        finally:
            self.lock_manager.release(handle, clear_metadata=False)

    def acquire_with_metadata(self, metadata: dict[str, object], *, nonblocking: bool = True) -> TextIO:
        """Acquire lock and write initial metadata."""
        handle = self.lock_manager.acquire(nonblocking=nonblocking)
        try:
            self.lock_manager.write_locked_metadata(handle, metadata)
            return handle
        except Exception:
            self.lock_manager.release(handle, clear_metadata=True)
            raise

    def release_with_cleanup(self, handle: TextIO, *, clear_metadata: bool = True) -> None:
        """Release lock and optionally clear metadata."""
        self.lock_manager.release(handle, clear_metadata=clear_metadata)

    def create_base_metadata(self, pid: int, extra: dict[str, object] | None = None) -> dict[str, object]:
        """Create base metadata for a process lock."""
        now = utcnow()
        metadata = {
            "pid": pid,
            "started_at": now,
            "heartbeat_at": now,
        }
        if extra:
            metadata.update(extra)
        return metadata

    def update_heartbeat(self, handle: TextIO, extra_updates: dict[str, object] | None = None) -> None:
        """Update heartbeat timestamp in locked metadata."""
        metadata = self.read_locked_metadata(handle)
        metadata["heartbeat_at"] = utcnow()
        if extra_updates:
            metadata.update(extra_updates)
        self.write_locked_metadata(handle, metadata)

    def save_process_state(self, workspace: Path, payload: dict[str, object], *, status: str = "running") -> None:
        """Save process state to runtime store."""
        from litehive.state.store import runtime_store  # noqa: PLC0415

        runtime_store(workspace).save_process_state(
            self.process_name,
            status=status,
            payload=payload,
        )

    def clear_process_state(self, workspace: Path) -> None:
        """Clear process state from runtime store."""
        from litehive.state.store import runtime_store  # noqa: PLC0415

        runtime_store(workspace).clear_process_state(self.process_name)

    def clear_stale_state(self, workspace: Path, *, expected_pid: int | None = None) -> bool:
        """Clear stale lock metadata and process state if conditions are met."""
        cleared = self.clear_metadata_if_unlocked(
            expected_pid=expected_pid,
            require_stale_pid=True,
        )
        if cleared:
            self.clear_process_state(workspace)
        return cleared
