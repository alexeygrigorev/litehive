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
    """
    Single owner for workspace lockfile metadata.

    Wraps the runner and daemon lockfiles so that PID-based liveness
    checks, stale-lockfile cleanup, flock acquisition, and JSON payload
    I/O share one implementation instead of being re-derived in each
    subsystem; ``ProcessLockManager`` layers process-specific policy on
    top of this.
    """

    path: Path
    """Lockfile path; parent directories are created on first open."""
    pid_is_alive: Callable[[object], bool]
    """Liveness oracle that accepts a PID and returns whether it is still running."""
    held_in_process: Callable[[], bool] | None = None
    """Optional callback returning True when the current process already holds the lock."""
    pid_field: str = "pid"
    """JSON key inside the lockfile that carries the owner PID."""
    fsync_writes: bool = False
    """When True, fsync after metadata writes so they survive a crash."""

    def _is_held_in_process(self) -> bool:
        """
        Ask the per-subsystem callback whether this process holds the lock.

        Lets ``is_active`` short-circuit the flock probe when the current
        Python process is the holder — flock is per-file-descriptor, so a
        re-entrant ``acquire`` from the same process would otherwise
        succeed and confuse ownership tracking.
        """
        if self.held_in_process is None:
            return False
        return self.held_in_process()

    def _parse_metadata_text(self, text: str, strict: bool) -> dict[str, object] | None:
        """
        Decode the lockfile JSON envelope, returning ``None`` on bad files.

        Treating empty/null payloads as ``{}`` lets callers distinguish a
        present-but-uninitialised lockfile from a corrupt one;
        ``strict=True`` re-raises ``json.JSONDecodeError`` so callers that
        just wrote the file can spot a write failure instead of silently
        treating their own write as foreign.
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
        """
        Read the lockfile JSON without taking the flock.

        Returns ``None`` for missing or unreadable files. Used by
        status/observability paths that want to peek at "who owns the
        lock" without blocking on it; ``strict=True`` is for callers that
        have already validated the file should exist.
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
        """
        Read metadata while the caller already holds the flock.

        Never raises on bad JSON because the caller is the only writer
        once the flock is held; if the payload is unreadable here that's
        a write-bug to surface, but only after the caller's own write
        completes — returning ``{}`` keeps the caller alive.
        """
        handle.seek(0)
        payload = self._parse_metadata_text(handle.read(), strict=False)
        if payload is None:
            return {}
        return payload

    def write_locked_metadata(self, handle: TextIO, payload: Mapping[str, object]) -> None:
        """
        Replace the lockfile contents with ``payload`` under the held flock.

        Truncate-then-write is intentional: callers see a complete
        document or an empty file, never a half-overwritten one.
        ``fsync_writes`` exists for the daemon lock where survival
        across crashes matters; the runner lock skips fsync to keep
        heartbeat updates cheap.
        """
        handle.seek(0)
        handle.truncate()
        json.dump(dict(payload), handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        if self.fsync_writes:
            os.fsync(handle.fileno())

    def clear_locked_metadata(self, handle: TextIO) -> None:
        """
        Empty the lockfile while still holding the flock.

        Used on clean release so the next probe sees a present-but-empty
        file (signals "nobody home") rather than a stale identity payload
        that would be treated as a still-live owner.
        """
        handle.seek(0)
        handle.truncate()
        handle.flush()
        if self.fsync_writes:
            os.fsync(handle.fileno())

    def open(self) -> TextIO:
        """
        Open the lockfile in append+read mode, creating the parent dir.

        Append-mode is what makes a missing file safe (the create happens
        through ``open(..., "a+")``); creating the parent directory means
        first-use on a fresh workspace doesn't have to pre-provision the
        directory before the lock is taken.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path.open("a+", encoding="utf-8")

    def lock(self, handle: TextIO, nonblocking: bool) -> None:
        """
        Take an exclusive flock on ``handle``.

        Raises ``BlockingIOError`` in nonblocking mode when contended.
        Thin wrapper that ``acquire`` and ``is_active`` both route through
        so the flock-mode flag is normalised in one place and tests can
        swap the behaviour by subclassing.
        """
        if nonblocking:
            nonblocking_flag = fcntl.LOCK_NB
        else:
            nonblocking_flag = 0
        mode = fcntl.LOCK_EX | nonblocking_flag
        fcntl.flock(handle.fileno(), mode)

    def unlock(self, handle: TextIO) -> None:
        """
        Release the flock on ``handle``.

        Kept as a method instead of inlining ``fcntl.flock`` so subclasses
        and tests can intercept release without monkey-patching ``fcntl``;
        the symmetric pair to ``lock``.
        """
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self, nonblocking: bool, cleanup_stale_inode: bool = False) -> TextIO:
        """
        Open and flock the lockfile, optionally unlinking a stale inode first.

        ``cleanup_stale_inode`` is for the runner-startup path: when a
        previous runner died without releasing, deleting the inode before
        flocking gives the new owner a fresh file rather than reusing a
        ghost handle the dead process may still hold.
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
        """
        Drop the flock and close the handle, optionally clearing metadata first.

        Layered try/finally guarantees the unlock happens even if metadata
        clearing raises — leaving a process alive while still holding the
        lock would block every subsequent runner/daemon start until
        manual intervention.
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
        """
        Probe whether some process currently holds the flock, without blocking.

        Used by status views and from-startup checks to decide if a
        previous runner/daemon is still live; try-and-release is the only
        portable way to ask "is this lock held?" via fcntl, and the
        in-process short-circuit keeps a self-probe from spuriously
        blocking.
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
        """
        True only when ``metadata`` names a PID the OS still considers alive.

        ``clear_metadata_if_unlocked`` uses this to decide whether a
        non-empty metadata blob represents a live owner or leftover state
        that's safe to scrub; getting this wrong would either steal a live
        owner's lock or leave a dead one's metadata in place forever.
        """
        if not metadata:
            return False
        pid = metadata.get(self.pid_field)
        return isinstance(pid, int) and self.pid_is_alive(pid)

    def pid_is_stale(self) -> bool:
        """
        True only when the lockfile names a PID that no longer exists.

        Keeping "missing metadata" and "missing pid" as not-stale is
        deliberate: callers (recovery, runner startup) should only react
        to a confirmed dead PID, not to absent files they themselves may
        be about to populate.
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
        """
        Truncate stale lockfile metadata only when no live process holds the flock.

        Used by the recovery flow to scrub leftover runner/daemon entries
        from a crashed previous run without racing a currently-live owner;
        ``expected_pid`` and ``require_stale_pid`` are extra gates so a
        recovery pass cannot clear somebody else's still-valid lock.
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
        """
        Unlink the lockfile inode when its recorded PID is dead.

        Used before runner startup; unlinking rather than just truncating
        is what gives a fresh runner a brand-new inode so leftover open
        handles from the previous owner cannot accidentally affect the
        new lock through the same inode.
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
