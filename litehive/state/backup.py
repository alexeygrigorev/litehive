"""Workspace database backup helpers."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
import gzip
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from litehive.workspace import Workspace

_BACKUP_NAME_RE = re.compile(r"^data-(\d{4}-\d{2}-\d{2}T\d{2})\.db\.gz$")


@dataclass(frozen=True)
class WorkspaceBackup:
    """
    One successfully captured workspace database snapshot.

    Returned by ``list_workspace_backups``, ``create_workspace_backup``,
    and ``restore_workspace_backup`` so operator-facing surfaces can show
    the timestamp and size without re-deriving them from the filename.
    """

    timestamp: str
    """Hour-resolution UTC timestamp embedded in the backup filename."""
    created_at: datetime
    """Parsed datetime of the backup's capture time."""
    path: Path
    """Full filesystem path to the gzip-compressed backup file."""
    size_bytes: int
    """Compressed size of the backup file in bytes."""


def _backup_timestamp(when: datetime) -> str:
    """
    Format a datetime as the hour-resolution UTC timestamp for backup names.

    The hourly granularity is what makes ``create_scheduled_workspace_backup``
    idempotent across the day's first run plus retries: the same hour
    cannot yield two different filenames so a retry within the same hour
    overwrites itself.
    """
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H")


def _parse_backup_path(path: Path) -> WorkspaceBackup | None:
    """
    Decode a candidate backup file path into a ``WorkspaceBackup``.

    Used by ``list_workspace_backups`` and ``create_workspace_backup``:
    any file in ``backups/`` that doesn't match the canonical naming
    pattern is treated as foreign and silently ignored, keeping the
    listing safe to point at directories with mixed content.
    """
    match = _BACKUP_NAME_RE.match(path.name)
    if match is None or not path.is_file():
        return None
    timestamp = match.group(1)
    created_at = datetime.strptime(timestamp, "%Y-%m-%dT%H").replace(tzinfo=UTC)
    return WorkspaceBackup(
        timestamp=timestamp,
        created_at=created_at,
        path=path,
        size_bytes=path.stat().st_size,
    )


class WorkspaceBackupService:
    """
    Workspace-bound database backup service.

    Owns listing, retention, creation, scheduled creation, and restore
    for the SQLite runtime database.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Bind the backup service to a workspace.

        The workspace provides the runtime directory layout (``data.db``
        location, ``backups/`` subdirectory) that all backup operations
        use; the service is stateless between calls.
        """
        self.workspace = workspace

    def list_backups(self) -> list[WorkspaceBackup]:
        """
        Enumerate persisted DB snapshots, newest first.
        """
        backup_dir = self.workspace.runtime_path("backups")
        if not backup_dir.exists():
            return []
        backups = [_parse_backup_path(path) for path in backup_dir.iterdir()]
        return sorted(
            [backup for backup in backups if backup is not None],
            key=lambda backup: backup.created_at,
            reverse=True,
        )

    def prune(self) -> list[Path]:
        """
        Apply the daily-then-weekly retention policy.
        """
        backups = self.list_backups()
        keep_paths: set[Path] = set()
        kept_days: set[date] = set()
        kept_weeks: set[tuple[int, int]] = set()

        for backup in backups:
            backup_day = backup.created_at.date()
            if len(kept_days) < 7:
                if backup_day in kept_days:
                    continue
                kept_days.add(backup_day)
                keep_paths.add(backup.path)
                continue

            week_key = (backup.created_at.isocalendar().year, backup.created_at.isocalendar().week)
            if week_key in kept_weeks or len(kept_weeks) >= 4:
                continue
            kept_weeks.add(week_key)
            keep_paths.add(backup.path)

        deleted: list[Path] = []
        for backup in backups:
            if backup.path in keep_paths:
                continue
            backup.path.unlink(missing_ok=True)
            deleted.append(backup.path)
        return deleted

    def backup_path(self, when: datetime) -> Path:
        """
        Compute the canonical ``backups/data-<timestamp>.db.gz`` path.
        """
        return self.workspace.runtime_path("backups") / f"data-{_backup_timestamp(when)}.db.gz"

    def create(self, when: datetime | None = None) -> WorkspaceBackup:
        """
        Take a consistent gzip snapshot of ``data.db`` via SQLite's online backup.
        """
        when = when or datetime.now(UTC)
        backup_dir = self.workspace.runtime_path("backups")
        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = self.backup_path(when)

        with tempfile.NamedTemporaryFile(
            suffix=".db",
            dir=backup_dir,
            delete=False,
        ) as db_handle:
            temp_db_path = Path(db_handle.name)
        with tempfile.NamedTemporaryFile(
            suffix=".db.gz",
            dir=backup_dir,
            delete=False,
        ) as gz_handle:
            temp_gz_path = Path(gz_handle.name)

        try:
            source = sqlite3.connect(self.workspace.runtime_path("data.db"))
            target = sqlite3.connect(temp_db_path)
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()
                source.close()

            with (
                temp_db_path.open("rb") as source_handle,
                gzip.open(temp_gz_path, "wb", compresslevel=9) as gzip_handle,
            ):
                while True:
                    chunk = source_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    gzip_handle.write(chunk)

            os.replace(temp_gz_path, destination)
        finally:
            temp_db_path.unlink(missing_ok=True)
            temp_gz_path.unlink(missing_ok=True)

        self.prune()
        backup = _parse_backup_path(destination)
        assert backup is not None
        return backup

    def create_scheduled(self, now: datetime | None = None) -> WorkspaceBackup | None:
        """
        Take a backup at most once per calendar day.
        """
        now = now or datetime.now(UTC)
        backups = self.list_backups()
        if backups and backups[0].created_at.date() == now.date():
            return None
        return self.create(when=now)

    def restore(self, timestamp: str) -> WorkspaceBackup:
        """
        Replace ``data.db`` with a previously-captured snapshot.
        """
        backup = next((item for item in self.list_backups() if item.timestamp == timestamp), None)
        if backup is None:
            raise ValueError(f"backup not found for timestamp {timestamp}")

        database_path = self.workspace.runtime_path("data.db")
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            suffix=".db",
            dir=database_path.parent,
            delete=False,
        ) as handle:
            temp_restore_path = Path(handle.name)

        try:
            with gzip.open(backup.path, "rb") as source_handle, temp_restore_path.open("wb") as target_handle:
                while True:
                    chunk = source_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    target_handle.write(chunk)
            os.replace(temp_restore_path, database_path)
        finally:
            temp_restore_path.unlink(missing_ok=True)

        for suffix in ("-wal", "-shm"):
            database_path.with_name(database_path.name + suffix).unlink(missing_ok=True)
        return backup
