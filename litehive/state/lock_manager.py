"""Shared flock/YAML helpers for workspace-local process lock files."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
from typing import TextIO

import yaml


@dataclass(slots=True)
class WorkspaceLockManager:
    path: Path
    pid_is_alive: Callable[[object], bool]
    held_in_process: Callable[[], bool] | None = None
    pid_field: str = "pid"
    fsync_writes: bool = False

    def _is_held_in_process(self) -> bool:
        return False if self.held_in_process is None else self.held_in_process()

    def _parse_metadata_text(self, text: str, *, strict: bool) -> dict[str, object] | None:
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            if strict:
                raise
            return None
        if document is None:
            return {}
        if not isinstance(document, dict):
            return None
        return dict(document)

    def read_metadata(self, *, strict: bool = False) -> dict[str, object] | None:
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
        handle.seek(0)
        payload = self._parse_metadata_text(handle.read(), strict=False)
        return {} if payload is None else payload

    def write_locked_metadata(self, handle: TextIO, payload: Mapping[str, object]) -> None:
        handle.seek(0)
        handle.truncate()
        yaml.safe_dump(dict(payload), handle, sort_keys=False)
        handle.flush()
        if self.fsync_writes:
            os.fsync(handle.fileno())

    def clear_locked_metadata(self, handle: TextIO) -> None:
        handle.seek(0)
        handle.truncate()
        handle.flush()
        if self.fsync_writes:
            os.fsync(handle.fileno())

    def open(self) -> TextIO:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path.open("a+", encoding="utf-8")

    def lock(self, handle: TextIO, *, nonblocking: bool) -> None:
        mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(handle.fileno(), mode)

    def unlock(self, handle: TextIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self, *, nonblocking: bool, cleanup_stale_inode: bool = False) -> TextIO:
        if cleanup_stale_inode:
            self.remove_stale_lockfile()
        handle = self.open()
        try:
            self.lock(handle, nonblocking=nonblocking)
        except Exception:
            handle.close()
            raise
        return handle

    def release(self, handle: TextIO, *, clear_metadata: bool) -> None:
        try:
            if clear_metadata:
                self.clear_locked_metadata(handle)
        finally:
            try:
                self.unlock(handle)
            finally:
                handle.close()

    def is_active(self) -> bool:
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
        if not metadata:
            return False
        pid = metadata.get(self.pid_field)
        return isinstance(pid, int) and self.pid_is_alive(pid)

    def metadata_status(self) -> tuple[dict[str, object] | None, str]:
        metadata = self.read_metadata()
        if not metadata:
            return None, "stopped"
        if self.is_active() or self._pid_is_live(metadata):
            return dict(metadata), "running"
        return dict(metadata), "stale"

    def pid_is_stale(self) -> bool:
        metadata = self.read_metadata()
        if not metadata:
            return False
        pid = metadata.get(self.pid_field)
        if pid is None:
            return False
        return not self.pid_is_alive(pid)

    def clear_metadata_if_unlocked(
        self,
        *,
        expected_pid: int | None = None,
        require_stale_pid: bool = False,
    ) -> bool:
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
