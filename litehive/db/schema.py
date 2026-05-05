"""SQLite schema migration runtime for Litehive workspace databases."""

from dataclasses import dataclass
from datetime import UTC, datetime
import importlib.resources
import json
import logging
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator
from typing import TypeAlias

from pydantic import ValidationError

from litehive.config.paths import workspace_path
from litehive.domain.task import TaskIntentRecord, TaskStateRecord
from litehive.state.rebuild_safety import assert_database_rebuild_safe, backup_database_before_rebuild

MIGRATIONS_PACKAGE = "litehive.db.migrations"
logger = logging.getLogger(__name__)
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
    "worktrees",
    "pipeline_transitions",
    "pipeline_journal",
    "pipeline_task_state",
    "pipeline_sessions",
    "task_audit_log",
}
_REQUIRED_TABLES_BY_MIGRATION = {
    4: {"recovery_reports"},
    5: {"task_intent"},
    6: {"runtime_settings", "runtime_settings_audit_log"},
    7: {"runtime_process_state"},
    9: {"attention_log"},
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
        """Wrap the SQL error from a single failed migration with the migration metadata so the ``litehive db migrate`` CLI can show which migration name+version blew up rather than only the underlying SQLite error string."""
        super().__init__(f"migration {migration.name} failed: {cause}")
        self.migration = migration
        self.cause = cause


def _utcnow() -> str:
    """Format an ``applied_at`` timestamp the way ``schema_migrations`` rows expect (second-precision Z-suffixed)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _task_intent_column_values(
    intent: TaskIntentRecord,
    state: TaskStateRecord | None = None,
) -> dict[str, str]:
    """Project a TaskIntentRecord/TaskStateRecord pair onto the flat column shape used by migration 7's task_intent backfill."""
    return {
        "slug": intent.slug,
        "title": intent.title,
        "created_at": intent.created_at,
        "priority": intent.priority,
        "goal": intent.goal,
        "acceptance_criteria_json": json.dumps(intent.acceptance_criteria, sort_keys=True),
        "constraints_json": json.dumps(intent.constraints, sort_keys=True),
        "plan_json": json.dumps(intent.plan, sort_keys=True),
        "dependencies_json": json.dumps(intent.depends_on, sort_keys=True),
        "provenance_json": json.dumps(
            {} if intent.created_from is None else intent.created_from.model_dump(mode="json"),
            sort_keys=True,
        ),
        "lifecycle_status": "queued" if state is None else str(state.status),
        "pipeline_status": "backlog" if state is None else str(state.pipeline_status),
    }


def _sync_task_intent_columns(connection: sqlite3.Connection) -> None:
    """Backfill the denormalized ``task_intent`` columns from each row's JSON payload; called after migration 7 runs so list/filter queries see the new columns populated for every existing task instead of needing a one-shot operator step."""
    rows = connection.execute(
        """
        SELECT intent.task_id, intent.payload AS intent_payload, state.payload AS state_payload
        FROM task_intent AS intent
        LEFT JOIN task_state AS state ON state.task_id = intent.task_id
        """
    ).fetchall()
    for row in rows:
        try:
            intent_payload = json.loads(str(row["intent_payload"]))
            intent = TaskIntentRecord.model_validate(intent_payload)
            state = None
            if row["state_payload"] is not None:
                state_payload = json.loads(str(row["state_payload"]))
                state = TaskStateRecord.model_validate(state_payload)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            logger.warning("Skipping invalid task_intent column backfill for %s: %s", row["task_id"], exc)
            continue
        values = _task_intent_column_values(intent, state)
        connection.execute(
            """
            UPDATE task_intent
            SET
                slug = ?,
                title = ?,
                created_at = ?,
                priority = ?,
                goal = ?,
                acceptance_criteria_json = ?,
                constraints_json = ?,
                plan_json = ?,
                dependencies_json = ?,
                provenance_json = ?,
                lifecycle_status = ?,
                pipeline_status = ?
            WHERE task_id = ?
            """,
            (
                values["slug"],
                values["title"],
                values["created_at"],
                values["priority"],
                values["goal"],
                values["acceptance_criteria_json"],
                values["constraints_json"],
                values["plan_json"],
                values["dependencies_json"],
                values["provenance_json"],
                values["lifecycle_status"],
                values["pipeline_status"],
                row["task_id"],
            ),
        )


def _migration_resources():
    """Locate the bundled migration directory through ``importlib.resources``; isolated as a one-liner so tests can monkey-patch a single symbol when they need to substitute a fixture set of migrations."""
    return importlib.resources.files(MIGRATIONS_PACKAGE)


def available_migrations() -> tuple[Migration, ...]:
    """Discover bundled SQL migrations; called by the migration CLI and by ``apply_pending_migrations`` to compute the apply plan."""
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
    """Single point that applies the project's connection pragmas (foreign keys, durability tradeoffs) so migration code and ``connect_workspace_db`` cannot diverge."""
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
    """Bootstrap the bookkeeping table on a fresh db so the first migration query does not error before migration 1 has run."""
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
    """Return the version set used by both ``migration_status`` (status reporting) and ``apply_pending_migrations`` (to skip already-applied migrations)."""
    _ensure_schema_migrations_table(connection)
    rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return {int(row["version"]) for row in rows}


def _applied_migration_rows(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    """Return ``(version, name)`` for each applied migration in order; ``_database_requires_rebuild`` uses the names (not just versions) to detect a renamed migration that would otherwise look already-applied by version alone."""
    _ensure_schema_migrations_table(connection)
    rows = connection.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    return [(int(row["version"]), str(row["name"])) for row in rows]


def _has_required_baseline_tables(
    connection: sqlite3.Connection,
    applied_versions: set[int],
) -> bool:
    """Confirm every table the applied migrations should have created is actually present; ``_database_requires_rebuild`` consults this so a partially-applied or table-dropped DB is treated as needing a rebuild rather than silently limping forward."""
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    tables = {str(row["name"]) for row in rows}
    required_tables = set(_BASELINE_REQUIRED_TABLES)
    for version, migration_tables in _REQUIRED_TABLES_BY_MIGRATION.items():
        if version in applied_versions:
            required_tables.update(migration_tables)
    return required_tables <= tables


def _migration_history_matches_prefix(
    applied: list[tuple[int, str]],
    available: tuple[Migration, ...],
) -> bool:
    """Return whether the DB's applied-migration log is a strict prefix of the bundled migrations; used by ``_database_requires_rebuild`` to detect a renamed/reordered/diverged history that cannot be reconciled by patching forward."""
    if len(applied) > len(available):
        return False
    expected_prefix = [(migration.version, migration.name) for migration in available[: len(applied)]]
    return applied == expected_prefix


def _database_requires_rebuild(db_path: Path, migrations: tuple[Migration, ...]) -> bool:
    """Detect a DB whose history has diverged from the bundled migrations (e.g. a renamed migration) so ``apply_pending_migrations`` can rebuild it instead of trying to patch in place."""
    if not db_path.exists():
        return False
    try:
        with _open_connection(db_path) as connection:
            applied = _applied_migration_rows(connection)
            if not _migration_history_matches_prefix(applied, migrations):
                return True
            applied_versions = {version for version, _name in applied}
            if applied and not _has_required_baseline_tables(connection, applied_versions):
                return True
    except sqlite3.DatabaseError:
        return True
    return False


def migration_status(root: Path) -> MigrationStatus:
    """Read-only view used by the ``litehive db status`` CLI; does not mutate the database."""
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
    if applied:
        current_version = applied[-1].version
    else:
        current_version = 0
    return MigrationStatus(
        current_version=current_version,
        applied_migrations=tuple(applied),
        pending_migrations=tuple(pending),
    )


def apply_pending_migrations(root: Path, dry_run: bool = False) -> MigrationPlan:
    """Bring the workspace DB up to the bundled schema; called by the migration CLI and lazily by ``connect_workspace_db`` on first open per-process."""
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
        assert_database_rebuild_safe(root, db_path, operation="migration-triggered database rebuild")
        backup_database_before_rebuild(root, db_path, label="before-migration-rebuild")
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
            migration_sql = migration.sql.strip()
            script = "\n".join(
                [
                    "BEGIN;",
                    migration_sql,
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
            if migration.version == 7:
                _sync_task_intent_columns(connection)
                connection.commit()
        applied = tuple(migrations)
    return MigrationPlan(applied_migrations=applied, pending_migrations=pending, dry_run=False)


_DbFingerprint: TypeAlias = tuple[int, int] | None


def _db_fingerprint(db_path: Path) -> _DbFingerprint:
    """Identify the file (dev/inode) so the in-process migration cache invalidates only when the DB is replaced, not on every write."""
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
    """Canonical cache key for ``MIGRATED_DB_PATHS``/``REBUILT_DB_PATHS``; resolves the path so two callers entering through different cwd-relative paths share one cache slot."""
    return str(db_path.resolve())


def consume_rebuilt_database_marker(root: Path) -> bool:
    """One-shot signal so callers (status/recovery output) can warn the operator exactly once that a rebuild happened this process."""
    key = _db_cache_key(workspace_path(root, "data.db"))
    if key not in REBUILT_DB_PATHS:
        return False
    REBUILT_DB_PATHS.remove(key)
    return True


@contextmanager
def connect_workspace_db(root: Path, migrate: bool = True) -> Iterator[sqlite3.Connection]:
    """The single entry point every workspace caller uses to talk to SQLite, so pragmas and on-demand migration stay consistent."""
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
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
