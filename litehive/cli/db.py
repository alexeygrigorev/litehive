"""CLI helpers for workspace database schema migrations."""

from litehive.config import resolve_workspace
from litehive.db import MigrationApplyError, apply_pending_migrations, migration_status


def cmd_db_status(workspace) -> int:
    workspace = resolve_workspace(None, workspace=workspace)
    status = migration_status(workspace)
    print(f"workspace: {workspace}")
    print(f"schema_version: {status.current_version}")
    print(f"applied_migrations: {len(status.applied_migrations)}")
    print(f"pending_migrations: {len(status.pending_migrations)}")
    for migration in status.pending_migrations:
        print(f"pending: {migration.name}")
    return 0


def cmd_db_migrate(workspace, dry_run: bool = False) -> int:
    workspace = resolve_workspace(None, workspace=workspace)
    try:
        plan = apply_pending_migrations(workspace, dry_run=dry_run)
    except MigrationApplyError as exc:
        print(f"db migrate failed: {exc}")
        return 1
    print(f"workspace: {workspace}")
    print(f"dry_run: {'yes' if plan.dry_run else 'no'}")
    if plan.pending_migrations:
        label = "would_apply" if plan.dry_run else "applied"
        for migration in plan.pending_migrations:
            print(f"{label}: {migration.name}")
    else:
        print("pending_migrations: 0")
    if not plan.dry_run:
        print(f"schema_version: {migration_status(workspace).current_version}")
    return 0
