"""Workspace database backup helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import gzip
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from litehive.config.paths import workspace_backups_dir, workspace_database_path

_BACKUP_NAME_RE = re.compile(r"^data-(\d{4}-\d{2}-\d{2}T\d{2})\.db\.gz$")


@dataclass(frozen=True)
class WorkspaceBackup:
    timestamp: str
    created_at: datetime
    path: Path
    size_bytes: int


def _backup_timestamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H")


def _backup_path(root: Path, when: datetime) -> Path:
    return workspace_backups_dir(root) / f"data-{_backup_timestamp(when)}.db.gz"


def _parse_backup_path(path: Path) -> WorkspaceBackup | None:
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


def list_workspace_backups(root: Path) -> list[WorkspaceBackup]:
    backup_dir = workspace_backups_dir(root)
    if not backup_dir.exists():
        return []
    backups = [_parse_backup_path(path) for path in backup_dir.iterdir()]
    return sorted(
        [backup for backup in backups if backup is not None],
        key=lambda backup: backup.created_at,
        reverse=True,
    )


def prune_workspace_backups(root: Path) -> list[Path]:
    backups = list_workspace_backups(root)
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


def create_workspace_backup(root: Path, *, when: datetime | None = None) -> WorkspaceBackup:
    when = when or datetime.now(UTC)
    backup_dir = workspace_backups_dir(root)
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = _backup_path(root, when)

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
        source = sqlite3.connect(workspace_database_path(root))
        target = sqlite3.connect(temp_db_path)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()

        with temp_db_path.open("rb") as source_handle, gzip.open(
            temp_gz_path, "wb", compresslevel=9
        ) as gzip_handle:
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                gzip_handle.write(chunk)

        os.replace(temp_gz_path, destination)
    finally:
        temp_db_path.unlink(missing_ok=True)
        temp_gz_path.unlink(missing_ok=True)

    prune_workspace_backups(root)
    backup = _parse_backup_path(destination)
    assert backup is not None
    return backup


def create_scheduled_workspace_backup(
    root: Path,
    *,
    now: datetime | None = None,
) -> WorkspaceBackup | None:
    now = now or datetime.now(UTC)
    backups = list_workspace_backups(root)
    if backups and backups[0].created_at.date() == now.date():
        return None
    return create_workspace_backup(root, when=now)


def restore_workspace_backup(root: Path, timestamp: str) -> WorkspaceBackup:
    backup = next((item for item in list_workspace_backups(root) if item.timestamp == timestamp), None)
    if backup is None:
        raise ValueError(f"backup not found for timestamp {timestamp}")

    database_path = workspace_database_path(root)
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
