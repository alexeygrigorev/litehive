"""SQLite schema migration runtime for Litehive workspace databases."""

from dataclasses import dataclass
from datetime import UTC, datetime
import importlib.resources
import os
import sqlite3
from pathlib import Path
from typing import TypeAlias

from litehive.config.paths import workspace_path

MIGRATIONS_PACKAGE = "litehive.db.migrations"
_BASELINE_REQUIRED_TABLES = {
    "schema_migrations",
    "pool_state",
    "queue",
    "task_state",
    "task_journal",
    "task_activity",
    "stage_reports",
    "hook_artifacts",
    "subagent_sessions",
    "events",
    "engine_monitoring",
    "attention",
    "worktrees",
    "pipeline_transitions",
    "pipeline_journal",
    "pipeline_task_state",
    "pipeline_sessions",
    "task_audit_log",
}


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


@dataclass(frozen=True)
class MigrationStatus:
    current_version: int
    applied_migrations: tuple[Migration, ...]
    pending_migrations: tuple[Migration, ...]


@dataclass(frozen=True)
class MigrationPlan:
    applied_migrations: tuple[Migration, ...]
    pending_migrations: tuple[Migration, ...]
    dry_run: bool


class MigrationApplyError(RuntimeError):
    """Raised when a schema migration fails."""

    def __init__(self, migration: Migration, cause: Exception) -> None:
        super().__init__(f"migration {migration.name} failed: {cause}")
        self.migration = migration
        self.cause = cause


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _migration_resources():
    return importlib.resources.files(MIGRATIONS_PACKAGE)


def available_migrations() -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    for entry in sorted(_migration_resources().iterdir(), key=lambda item: item.name):
        if not entry.name.endswith(".sql") or "_" not in entry.name:
            continue
        version_text, _ = entry.name.split("_", 1)
        if not version_text.isdigit():
            continue
        migrations.append(
            Migration(
                version=int(version_text),
                name=entry.name,
                sql=entry.read_text(encoding="utf-8"),
            )
        )
    return tuple(migrations)


def _open_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # In tests (LITEHIVE_SKIP_FSYNC set), trade durability for speed. Saves
    # tens of ms per transaction which compounds into minutes across 986 tests.
    if os.environ.get("LITEHIVE_SKIP_FSYNC"):
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA journal_mode = MEMORY")
        connection.execute("PRAGMA temp_store = MEMORY")
    return connection


def _ensure_schema_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def _applied_versions(connection: sqlite3.Connection) -> set[int]:
    _ensure_schema_migrations_table(connection)
    rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return {int(row["version"]) for row in rows}


def _applied_migration_rows(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    _ensure_schema_migrations_table(connection)
    rows = connection.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    return [(int(row["version"]), str(row["name"])) for row in rows]


def _has_required_baseline_tables(connection: sqlite3.Connection) -> bool:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    tables = {str(row["name"]) for row in rows}
    return _BASELINE_REQUIRED_TABLES <= tables


def _migration_history_matches_prefix(
    applied: list[tuple[int, str]],
    available: tuple[Migration, ...],
) -> bool:
    if len(applied) > len(available):
        return False
    expected_prefix = [(migration.version, migration.name) for migration in available[: len(applied)]]
    return applied == expected_prefix


def _database_requires_rebuild(db_path: Path, migrations: tuple[Migration, ...]) -> bool:
    if not db_path.exists():
        return False
    try:
        with _open_connection(db_path) as connection:
            applied = _applied_migration_rows(connection)
            if not _migration_history_matches_prefix(applied, migrations):
                return True
            if applied and not _has_required_baseline_tables(connection):
                return True
    except sqlite3.DatabaseError:
        return True
    return False


def migration_status(root: Path) -> MigrationStatus:
    db_path = workspace_path(root, "data.db")
    migrations = available_migrations()
    applied: list[Migration] = []
    pending: list[Migration] = []
    with _open_connection(db_path) as connection:
        applied_versions = _applied_versions(connection)
    for migration in migrations:
        if migration.version in applied_versions:
            applied.append(migration)
        else:
            pending.append(migration)
    current_version = applied[-1].version if applied else 0
    return MigrationStatus(
        current_version=current_version,
        applied_migrations=tuple(applied),
        pending_migrations=tuple(pending),
    )


def apply_pending_migrations(root: Path, *, dry_run: bool = False) -> MigrationPlan:
    db_path = workspace_path(root, "data.db")
    migrations = available_migrations()
    if _database_requires_rebuild(db_path, migrations):
        if dry_run:
            return MigrationPlan(
                applied_migrations=(),
                pending_migrations=migrations,
                dry_run=True,
            )
        key = _db_cache_key(db_path)
        db_path.unlink(missing_ok=True)
        MIGRATED_DB_PATHS.pop(key, None)
        REBUILT_DB_PATHS.add(key)
    with _open_connection(db_path) as connection:
        applied_versions = _applied_versions(connection)
        pending = tuple(migration for migration in migrations if migration.version not in applied_versions)
        if dry_run or not pending:
            applied = tuple(migration for migration in migrations if migration.version in applied_versions)
            return MigrationPlan(applied_migrations=applied, pending_migrations=pending, dry_run=dry_run)
        for migration in pending:
            applied_at = _utcnow()
            script = "\n".join(
                [
                    "BEGIN;",
                    migration.sql.strip(),
                    (
                        "INSERT INTO schema_migrations (version, name, applied_at) "
                        f"VALUES ({migration.version}, '{migration.name}', '{applied_at}');"
                    ),
                    "COMMIT;",
                ]
            )
            try:
                connection.executescript(script)
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise MigrationApplyError(migration, exc) from exc
        applied = tuple(migrations)
    return MigrationPlan(applied_migrations=applied, pending_migrations=pending, dry_run=False)


_DbFingerprint: TypeAlias = tuple[int, int] | None


def _db_fingerprint(db_path: Path) -> _DbFingerprint:
    try:
        stat = db_path.stat()
    except OSError:
        return None
    # Track database identity, not normal content churn. Size and mtime change
    # on every successful write, which would defeat the migration cache.
    return (stat.st_dev, stat.st_ino)


MIGRATED_DB_PATHS: dict[str, _DbFingerprint] = {}
REBUILT_DB_PATHS: set[str] = set()


def _db_cache_key(db_path: Path) -> str:
    return str(db_path.resolve())


def consume_rebuilt_database_marker(root: Path) -> bool:
    key = _db_cache_key(workspace_path(root, "data.db"))
    if key not in REBUILT_DB_PATHS:
        return False
    REBUILT_DB_PATHS.remove(key)
    return True


def connect_workspace_db(root: Path, *, migrate: bool = True) -> sqlite3.Connection:
    db_path = workspace_path(root, "data.db")
    if migrate:
        # In-process cache keyed on the absolute db_path (not root), because
        # XDG_DATA_HOME changes can move the db even when root stays the same.
        # The cached value follows database identity, so normal writes do not
        # force another migration check on the next open.
        key = _db_cache_key(db_path)
        fingerprint = _db_fingerprint(db_path)
        if key not in MIGRATED_DB_PATHS or MIGRATED_DB_PATHS[key] != fingerprint:
            apply_pending_migrations(root)
            MIGRATED_DB_PATHS[key] = _db_fingerprint(db_path)
    connection = _open_connection(db_path)
    if not migrate:
        _ensure_schema_migrations_table(connection)
    return connection
