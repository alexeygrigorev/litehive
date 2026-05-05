"""Shared flock helpers for workspace-local process lock files."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
from typing import TextIO


@dataclass(slots=True)
class WorkspaceLockManager:
    """Single owner for workspace lockfile metadata: flock acquisition plus JSON payload I/O.

    Wraps the runner and daemon lockfiles so that PID-based liveness checks,
    stale-lockfile cleanup, and metadata writes share one implementation
    instead of being re-derived in each subsystem.
    """

    path: Path
    pid_is_alive: Callable[[object], bool]
    held_in_process: Callable[[], bool] | None = None
    pid_field: str = "pid"
    fsync_writes: bool = False

    def _is_held_in_process(self) -> bool:
        """Ask the per-subsystem callback whether the lock is already held by this process.

        Lets ``is_active`` short-circuit the flock probe in cases where the
        current Python process is the holder — flock is per-file-descriptor,
        so a re-entrant ``acquire`` from the same process would otherwise
        succeed and confuse ownership tracking.
        """
        if self.held_in_process is None:
            return False
        return self.held_in_process()

    def _parse_metadata_text(self, text: str, strict: bool) -> dict[str, object] | None:
        """Decode the lockfile JSON envelope, returning ``None`` for "do not trust this file".

        Treating empty/null payloads as ``{}`` lets callers distinguish a
        present-but-uninitialized lockfile from a corrupt one; ``strict=True``
        re-raises so callers that just wrote the file can spot a write failure.
        """
        if not text.strip():
            return {}
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            if strict:
                raise
            return None
        if document is None:
            return {}
        if not isinstance(document, dict):
            return None
        return dict(document)

    def read_metadata(self, strict: bool = False) -> dict[str, object] | None:
        """Read the lockfile JSON without taking the flock; returns None for missing or unreadable files.

        Used by status/observability paths that want to peek at "who owns the
        lock" without blocking on the lock itself. ``strict=True`` is for
        callers that have already validated the file should exist.
        """
        if not self.path.exists():
            return None
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            if strict:
                raise
            return None
        return self._parse_metadata_text(text, strict=strict)

    def read_locked_metadata(self, handle: TextIO) -> dict[str, object]:
        """Read metadata while the caller already holds the flock; never raises on bad JSON."""
        handle.seek(0)
        payload = self._parse_metadata_text(handle.read(), strict=False)
        if payload is None:
            return {}
        return payload

    def write_locked_metadata(self, handle: TextIO, payload: Mapping[str, object]) -> None:
        """Replace the lockfile contents with ``payload`` while the caller holds the flock.

        Truncate-then-write is intentional: callers see a complete document or
        an empty file, never a half-overwritten one. ``fsync_writes`` exists
        for the daemon lock where survival across crashes matters.
        """
        handle.seek(0)
        handle.truncate()
        json.dump(dict(payload), handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        if self.fsync_writes:
            os.fsync(handle.fileno())

    def clear_locked_metadata(self, handle: TextIO) -> None:
        """Empty the lockfile while still holding the flock, used on clean release."""
        handle.seek(0)
        handle.truncate()
        handle.flush()
        if self.fsync_writes:
            os.fsync(handle.fileno())

    def open(self) -> TextIO:
        """Open the lockfile in append+read mode, creating the parent directory if needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path.open("a+", encoding="utf-8")

    def lock(self, handle: TextIO, nonblocking: bool) -> None:
        """Take an exclusive flock on ``handle``; raises ``BlockingIOError`` in nonblocking mode if contended.

        Thin wrapper that ``acquire`` and ``is_active`` route through so the
        flock-mode flag is normalized in one place and tests can swap the
        behaviour by subclassing.
        """
        if nonblocking:
            nonblocking_flag = fcntl.LOCK_NB
        else:
            nonblocking_flag = 0
        mode = fcntl.LOCK_EX | nonblocking_flag
        fcntl.flock(handle.fileno(), mode)

    def unlock(self, handle: TextIO) -> None:
        """Release the flock on ``handle``; the symmetric pair to ``lock``.

        Kept as a method (instead of inlining ``fcntl.flock``) so subclasses
        and tests can intercept release without monkey-patching ``fcntl``.
        """
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self, nonblocking: bool, cleanup_stale_inode: bool = False) -> TextIO:
        """Open and flock the lockfile, optionally unlinking a stale inode first.

        ``cleanup_stale_inode`` is for the runner-startup path: if a previous
        runner died without releasing, we delete its lockfile inode before
        flocking so the new owner gets a fresh file rather than reusing a
        ghost handle.
        """
        if cleanup_stale_inode:
            self.remove_stale_lockfile()
        handle = self.open()
        try:
            self.lock(handle, nonblocking=nonblocking)
        except Exception:
            handle.close()
            raise
        return handle

    def release(self, handle: TextIO, clear_metadata: bool) -> None:
        """Drop the flock and close the handle, optionally clearing metadata first.

        Layered try/finally guarantees the unlock happens even if metadata
        clearing raises — leaving a process alive while still holding the
        lock would block every subsequent runner/daemon start.
        """
        try:
            if clear_metadata:
                self.clear_locked_metadata(handle)
        finally:
            try:
                self.unlock(handle)
            finally:
                handle.close()

    def is_active(self) -> bool:
        """Probe whether some process currently holds the flock without blocking.

        Used by status views and from-startup checks to decide if a previous
        runner/daemon is still live. Try-and-release is the only portable way
        to ask "is this lock held?" via fcntl.
        """
        if self._is_held_in_process():
            return True
        handle = self.open()
        try:
            try:
                self.lock(handle, nonblocking=True)
            except BlockingIOError:
                return True
            self.unlock(handle)
            return False
        finally:
            handle.close()

    def _pid_is_live(self, metadata: Mapping[str, object] | None) -> bool:
        """Return True only if ``metadata`` names a PID that the OS still considers alive.

        Callers (``metadata_status``, ``clear_metadata_if_unlocked``) use this
        to decide whether a non-zero metadata blob represents a live owner or
        leftover state that's safe to scrub.
        """
        if not metadata:
            return False
        pid = metadata.get(self.pid_field)
        return isinstance(pid, int) and self.pid_is_alive(pid)

    def metadata_status(self) -> tuple[dict[str, object] | None, str]:
        """Classify the lock as stopped/running/stale by combining flock probe and PID liveness."""
        metadata = self.read_metadata()
        if not metadata:
            return None, "stopped"
        if self.is_active() or self._pid_is_live(metadata):
            return dict(metadata), "running"
        return dict(metadata), "stale"

    def pid_is_stale(self) -> bool:
        """Return True only when the lockfile names a PID that no longer exists.

        Keeping "missing metadata" and "missing pid" as not-stale is
        deliberate: callers (recovery, runner startup) should only react to a
        confirmed dead PID, not to absent files that they themselves may be
        about to populate.
        """
        metadata = self.read_metadata()
        if not metadata:
            return False
        pid = metadata.get(self.pid_field)
        if pid is None:
            return False
        return not self.pid_is_alive(pid)

    def clear_metadata_if_unlocked(
        self,
        expected_pid: int | None = None,
        require_stale_pid: bool = False,
    ) -> bool:
        """Truncate stale lockfile metadata only when no live process holds the flock.

        Used by the recovery flow to scrub leftover runner/daemon entries
        from a crashed previous run without ever racing a currently-live
        owner. The optional gates protect against clearing somebody else's
        valid lock.
        """
        if self._is_held_in_process() or not self.path.exists():
            return False
        handle = self.open()
        try:
            try:
                self.lock(handle, nonblocking=True)
            except BlockingIOError:
                return False
            metadata = self.read_locked_metadata(handle)
            if expected_pid is not None and metadata.get(self.pid_field) != expected_pid:
                self.unlock(handle)
                return False
            if require_stale_pid and self._pid_is_live(metadata):
                self.unlock(handle)
                return False
            self.clear_locked_metadata(handle)
            self.unlock(handle)
            return True
        finally:
            handle.close()

    def remove_stale_lockfile(self) -> bool:
        """Unlink the lockfile inode when its recorded PID is dead, used before runner startup.

        Unlinking (rather than just truncating) is what gives a fresh runner
        a brand-new inode, so leftover open handles from the previous owner
        can't accidentally affect the new lock.
        """
        metadata = self.read_metadata()
        if not metadata:
            return False
        pid = metadata.get(self.pid_field)
        if not isinstance(pid, int) or self.pid_is_alive(pid):
            return False
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            return False
        return True
