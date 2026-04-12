from __future__ import annotations

import click

from litehive.config import ensure_workspace, workspace_database_path
from litehive.daemon import get_workspace_daemon
from litehive.storage import (
    create_workspace_backup,
    list_workspace_backups,
    restore_workspace_backup,
)
from litehive.workspace.locking import runner_status


def cmd_backup_create(workspace):
    ensure_workspace(workspace)
    try:
        backup = create_workspace_backup(workspace)
    except Exception as exc:
        print(f"backup create failed: {exc}")
        return 1
    print(f"timestamp: {backup.timestamp}")
    print(f"path: {backup.path}")
    print(f"size_bytes: {backup.size_bytes}")
    return 0


def cmd_backup_list(workspace):
    ensure_workspace(workspace)
    backups = list_workspace_backups(workspace)
    print(f"backups: {len(backups)}")
    for backup in backups:
        print(f"timestamp: {backup.timestamp}")
        print(f"size_bytes: {backup.size_bytes}")
        print(f"path: {backup.path}")
    return 0


def cmd_backup_restore(timestamp, workspace, yes: bool = False):
    ensure_workspace(workspace)
    daemon = get_workspace_daemon(workspace)
    if daemon is not None:
        print("backup restore failed: workspace daemon is running")
        return 1

    runner = runner_status(workspace)
    if runner.status in {"running", "late"}:
        print("backup restore failed: workspace runner is active")
        return 1

    database_path = workspace_database_path(workspace)
    if not yes:
        confirmed = click.confirm(
            f"Restore backup {timestamp} and overwrite {database_path}?",
            default=False,
        )
        if not confirmed:
            print("restore cancelled")
            return 1

    try:
        backup = restore_workspace_backup(workspace, timestamp)
    except ValueError as exc:
        print(f"backup restore failed: {exc}")
        return 1
    except Exception as exc:
        print(f"backup restore failed: {exc}")
        return 1

    print(f"restored: {backup.timestamp}")
    print(f"path: {backup.path}")
    print(f"database: {database_path}")
    return 0
