"""SQLite schema migration runtime for Litehive workspace databases."""

from dataclasses import dataclass
from datetime import UTC, datetime
import importlib.resources
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import TypeAlias

import yaml
from pydantic import ValidationError

from litehive.config.paths import workspace_path
from litehive.domain.task import TaskIntentRecord, TaskRecord, TaskStateRecord

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
    "attention",
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


_TASK_DIR_RE = re.compile(r"^T-\d{4}-")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _legacy_task_yaml_paths(root: Path) -> list[Path]:
    tasks_dir = root / ".litehive" / "tasks"
    if not tasks_dir.exists():
        return []
    paths: list[Path] = []
    scan_roots = [tasks_dir]
    archive_dir = tasks_dir / "archive"
    if archive_dir.exists():
        scan_roots.append(archive_dir)
    for scan_root in scan_roots:
        for child in sorted(scan_root.iterdir()):
            if not child.is_dir() or _TASK_DIR_RE.match(child.name) is None:
                continue
            task_file = child / "task.yaml"
            if task_file.exists():
                paths.append(task_file)
    return paths


def _legacy_task_record(path: Path) -> TaskRecord:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("task file must contain a mapping")
    try:
        intent = TaskIntentRecord.model_validate(loaded)
        task = TaskRecord.from_intent_and_state(intent)
    except ValidationError:
        task = TaskRecord.model_validate(loaded)
    if path.parent.parent.name == "archive":
        task.status = "archived"
    return task


def _task_intent_column_values(
    intent: TaskIntentRecord,
    state: TaskStateRecord | None = None,
) -> dict[str, str]:
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


def _task_intent_backfill_sql(root: Path, *, updated_at: str) -> tuple[str, tuple[Path, ...]]:
    statements: list[str] = []
    migrated_paths: list[Path] = []
    for path in _legacy_task_yaml_paths(root):
        try:
            task = _legacy_task_record(path)
        except Exception as exc:
            logger.warning("Skipping invalid legacy task intent %s during migration 0005: %s", path, exc)
            continue
        intent = task.to_intent_record()
        state = task.to_storage_state_record()
        state_payload = json.dumps(state.model_dump(mode="json"), sort_keys=True)
        payload = json.dumps(intent.model_dump(mode="json"), sort_keys=True)
        statements.append(
            "\n".join(
                [
                    "INSERT INTO task_intent (task_id, payload, updated_at)",
                    f"VALUES ({_sql_literal(intent.id)}, {_sql_literal(payload)}, {_sql_literal(updated_at)})",
                    "ON CONFLICT(task_id) DO NOTHING;",
                    "INSERT INTO task_state (task_id, payload, updated_at)",
                    f"VALUES ({_sql_literal(intent.id)}, {_sql_literal(state_payload)}, {_sql_literal(updated_at)})",
                    "ON CONFLICT(task_id) DO NOTHING;",
                ]
            )
        )
        migrated_paths.append(path)
    return "\n".join(statements), tuple(migrated_paths)


def _remove_migrated_task_yaml_files(paths: tuple[Path, ...]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove migrated legacy task.yaml %s: %s", path, exc)


def _sync_task_intent_columns(connection: sqlite3.Connection) -> None:
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


def _legacy_archived_task_ids(root: Path) -> set[str]:
    archive_dir = root / ".litehive" / "tasks" / "archive"
    if not archive_dir.exists():
        return set()
    archived: set[str] = set()
    for child in archive_dir.iterdir():
        if not child.is_dir():
            continue
        match = re.match(r"^(T-\d{4})-", child.name)
        if match is not None:
            archived.add(match.group(1))
    index_path = archive_dir / "INDEX.csv"
    if index_path.exists():
        try:
            import csv

            with index_path.open(newline="", encoding="utf-8") as handle:
                archived.update(row["id"] for row in csv.DictReader(handle) if row.get("id"))
        except OSError:
            pass
    return archived


def _mark_legacy_archived_tasks(connection: sqlite3.Connection, root: Path) -> None:
    for task_id in sorted(_legacy_archived_task_ids(root)):
        row = connection.execute("SELECT payload FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            continue
        try:
            payload = json.loads(str(row["payload"]))
            state = TaskStateRecord.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            continue
        state.status = "archived"  # type: ignore[assignment]
        state.updated_at = state.updated_at or _utcnow()
        state_payload = state.model_dump(mode="json")
        connection.execute(
            """
            UPDATE task_state
            SET payload = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (json.dumps(state_payload, sort_keys=True), state.updated_at, task_id),
        )


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


def _has_required_baseline_tables(
    connection: sqlite3.Connection,
    applied_versions: set[int],
) -> bool:
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
            applied_versions = {version for version, _name in applied}
            if applied and not _has_required_baseline_tables(connection, applied_versions):
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
            migration_sql = migration.sql.strip()
            migrated_task_yaml_paths: tuple[Path, ...] = ()
            if migration.version == 5:
                task_intent_backfill_sql, migrated_task_yaml_paths = _task_intent_backfill_sql(
                    root,
                    updated_at=applied_at,
                )
                if task_intent_backfill_sql:
                    migration_sql = "\n".join([migration_sql, task_intent_backfill_sql])
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
            if migration.version == 5:
                _remove_migrated_task_yaml_files(migrated_task_yaml_paths)
            if migration.version == 7:
                _mark_legacy_archived_tasks(connection, root)
                _sync_task_intent_columns(connection)
                connection.commit()
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
