"""SQLite schema migration runtime for Litehive workspace databases."""


from dataclasses import dataclass
from datetime import UTC, datetime
import importlib.resources
import os
import sqlite3
from pathlib import Path

from litehive.config.paths import workspace_database_path

MIGRATIONS_PACKAGE = "litehive.db.migrations"


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


def migration_status(root: Path) -> MigrationStatus:
    db_path = workspace_database_path(root)
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
    db_path = workspace_database_path(root)
    migrations = available_migrations()
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


_MIGRATED_DB_PATHS: set[str] = set()


def connect_workspace_db(root: Path, *, migrate: bool = True) -> sqlite3.Connection:
    db_path = workspace_database_path(root)
    if migrate:
        # In-process cache keyed on the absolute db_path (not root), because
        # XDG_DATA_HOME changes can move the db even when root stays the same.
        # Cuts repeated sqlite opens from ~5ms to <1ms across a test suite.
        key = str(db_path.resolve())
        if key not in _MIGRATED_DB_PATHS:
            apply_pending_migrations(root)
            _MIGRATED_DB_PATHS.add(key)
    connection = _open_connection(db_path)
    _ensure_schema_migrations_table(connection)
    return connection
