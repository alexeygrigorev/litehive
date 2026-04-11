"""SQLite schema migration helpers for Litehive workspace databases."""

from .schema import (
    Migration,
    MigrationApplyError,
    MigrationPlan,
    MigrationStatus,
    apply_pending_migrations,
    connect_workspace_db,
    migration_status,
)

__all__ = [
    "Migration",
    "MigrationApplyError",
    "MigrationPlan",
    "MigrationStatus",
    "apply_pending_migrations",
    "connect_workspace_db",
    "migration_status",
]
