"""Shared process lock management for runners and daemons."""

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TextIO

from litehive.domain.common import utcnow
from litehive.state.lock_manager import WorkspaceLockManager
from litehive.state.store import RuntimeStore


@dataclass
class ProcessLockManager:
    """
    High-level process lock manager for runner and daemon processes.

    Adds process-policy semantics (named slot, PID-based liveness, runtime
    process registry mirroring) on top of the raw flock plumbing in
    ``WorkspaceLockManager``; the factory that creates this class wires
    the process-specific lock primitive so the constructor only receives
    ready collaborators.
    """

    process_name: str
    lock_manager: WorkspaceLockManager
    runtime_store: RuntimeStore | None = None

    def is_active(self) -> bool:
        """Probe whether the lock is currently held by any process."""
        return self.lock_manager.is_active()

    def read_metadata(self, strict: bool = False) -> dict[str, object] | None:
        """Read lock metadata without taking the flock; ``None`` for missing files."""
        return self.lock_manager.read_metadata(strict=strict)

    def read_locked_metadata(self, handle: TextIO) -> dict[str, object]:
        """Read metadata from an already-locked file handle (no JSON failures)."""
        return self.lock_manager.read_locked_metadata(handle)

    def write_locked_metadata(self, handle: TextIO, payload: Mapping[str, object]) -> None:
        """Replace metadata on an already-locked file handle."""
        self.lock_manager.write_locked_metadata(handle, payload)

    def clear_metadata_if_unlocked(
        self,
        expected_pid: int | None = None,
        require_stale_pid: bool = False,
    ) -> bool:
        """
        Clear lockfile metadata only when the optional gates pass.

        Forwarded to the inner manager so the runner and daemon both
        share the "don't clear somebody else's valid lock" guarantee.
        """
        return self.lock_manager.clear_metadata_if_unlocked(
            expected_pid=expected_pid,
            require_stale_pid=require_stale_pid,
        )

    def pid_is_stale(self) -> bool:
        """True only when the recorded PID is confirmed dead."""
        return self.lock_manager.pid_is_stale()

    def remove_stale_lockfile(self) -> bool:
        """Unlink the lockfile when its recorded PID is dead."""
        return self.lock_manager.remove_stale_lockfile()

    @contextmanager
    def open_locked(self, nonblocking: bool = True):
        """
        Context manager that acquires the lock and releases on exit.

        Used by daemon flows that want a short, scoped hold without
        clearing metadata on release; the metadata-clearing variant goes
        through ``release_with_cleanup`` instead.
        """
        handle = self.lock_manager.acquire(nonblocking=nonblocking)
        try:
            yield handle
        finally:
            self.lock_manager.release(handle, clear_metadata=False)

    def acquire_with_metadata(self, metadata: dict[str, object], nonblocking: bool = True) -> TextIO:
        """
        Acquire the lock and write the initial identity payload.

        The combined acquire+write is what makes "lock taken" and
        "identity recorded" appear atomically to readers; on failure the
        lock is released with metadata cleared so a partial acquire
        cannot leave half-written identity behind.
        """
        handle = self.lock_manager.acquire(nonblocking=nonblocking)
        try:
            self.lock_manager.write_locked_metadata(handle, metadata)
            return handle
        except Exception:
            self.lock_manager.release(handle, clear_metadata=True)
            raise

    def release_with_cleanup(self, handle: TextIO, clear_metadata: bool = True) -> None:
        """
        Release the lock and optionally clear metadata first.

        Symmetric pair to ``acquire_with_metadata``; clearing metadata is
        what marks "owner gone" to the next probe so a subsequent
        ``is_active`` returns ``False`` immediately rather than after a
        liveness check.
        """
        self.lock_manager.release(handle, clear_metadata=clear_metadata)

    def create_base_metadata(self, pid: int, extra: dict[str, object] | None = None) -> dict[str, object]:
        """
        Build the canonical metadata payload for a freshly acquired lock.

        Stamps pid plus matching ``started_at``/``heartbeat_at`` so the
        first liveness check after acquisition has a consistent baseline;
        callers merge ``extra`` on top for process-specific fields.
        """
        now = utcnow()
        metadata: dict[str, object] = {
            "pid": pid,
            "started_at": now,
            "heartbeat_at": now,
        }
        if extra:
            metadata.update(extra)
        return metadata

    def update_heartbeat(self, handle: TextIO, extra_updates: dict[str, object] | None = None) -> None:
        """
        Refresh ``heartbeat_at`` on the held metadata in place.

        Used by the daemon and runner heartbeat threads so liveness
        probes see a recent timestamp even when the foreground thread is
        blocked; ``extra_updates`` lets the caller piggyback related
        fields (e.g. active task pointer) onto the same write.
        """
        metadata = self.read_locked_metadata(handle)
        metadata["heartbeat_at"] = utcnow()
        if extra_updates:
            metadata.update(extra_updates)
        self.write_locked_metadata(handle, metadata)

    def save_process_state(self, payload: dict[str, object], status: str = "running") -> None:
        """
        Mirror process identity into the SQLite runtime store.

        Lets dashboards and read-only consumers see who owns the named
        slot without holding the flock; the lockfile remains the source
        of truth, but the SQLite row is the cheap-to-read mirror.
        """
        store = self._runtime_store()
        store.save_process_state(
            self.process_name,
            status=status,
            payload=payload,
        )

    def clear_process_state(self) -> None:
        """
        Drop the SQLite runtime mirror for this named process slot.

        Called on clean shutdown so dashboards stop reporting a phantom
        owner; the lockfile-side metadata may already be cleared, but
        leaving the SQLite mirror around would survive a restart and
        confuse the next status query.
        """
        self._runtime_store().clear_process_state(self.process_name)

    def clear_stale_state(self, expected_pid: int | None = None) -> bool:
        """
        Clear stale lock metadata and the SQLite mirror for this slot.

        Used on startup to reconcile a previous-run crash: only fires
        when the recorded PID is confirmed dead, so a still-live owner's
        metadata cannot be accidentally scrubbed.
        """
        cleared = self.clear_metadata_if_unlocked(
            expected_pid=expected_pid,
            require_stale_pid=True,
        )
        if cleared:
            self.clear_process_state()
        return cleared

    def _runtime_store(self) -> RuntimeStore:
        """
        Return the injected runtime store or fail loudly for write callers.
        """
        if self.runtime_store is None:
            raise RuntimeError(f"{self.process_name} process state store was not configured")
        return self.runtime_store
