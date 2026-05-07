"""
SQLite schema migration runtime for the workspace database.

Discovers bundled ``NNNN_name.sql`` migration files via
``importlib.resources``, applies pending ones in order inside one
transaction each, and tracks them in ``schema_migrations``.
``connect_workspace_db`` is the single open path every workspace
caller uses; it lazily applies migrations on first open per process
so callers don't have to remember to run a migration step.

Detects diverged history (renamed/reordered migrations) and rebuilds
the database after a guarded backup so a renamed migration cannot
silently look "already applied" by version while pointing at a
different schema.
"""

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
from litehive.domain.common import PipelineStatus, TaskStatus
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
    10: {"subagent_id_counters"},
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
    """
    Raised when a single schema migration fails.

    Carries the failing ``Migration`` and the underlying SQLite
    cause so ``litehive db migrate`` can render which migration
    name + version blew up rather than only the bare SQLite error
    text — the operator needs both to know whether to roll forward
    or revert.
    """

    def __init__(self, migration: Migration, cause: Exception) -> None:
        """
        Wrap the SQL error with migration metadata for operator output.

        The message intentionally puts the migration name first
        because that's what shows up on the CLI's first error
        line; the underlying ``cause`` is preserved on the
        attribute for tests and detailed diagnostics.
        """
        super().__init__(f"migration {migration.name} failed: {cause}")
        self.migration = migration
        self.cause = cause


def _utcnow() -> str:
    """
    Format an ``applied_at`` timestamp for ``schema_migrations`` rows.

    Second-precision with the ``Z`` suffix because that's the spelling
    the migration table has carried since version 1; switching to a
    different ISO variant would make freshly-applied rows sort
    differently from existing rows.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _task_intent_column_values(
    intent: TaskIntentRecord,
    state: TaskStateRecord | None = None,
) -> dict[str, str]:
    """
    Project a ``TaskIntentRecord`` / ``TaskStateRecord`` pair onto the migration-7 column shape.

    Migration 7 introduced denormalized columns on ``task_intent``
    (``slug``, ``priority``, ``goal``, ``…_json``) so list/filter
    queries don't have to parse JSON on every row. This helper
    produces the flat dict the backfill insert consumes; keeping
    the projection in Python avoids embedding pydantic-aware logic
    in SQL.
    """
    if intent.created_from is None:
        provenance_payload: dict = {}
    else:
        provenance_payload = intent.created_from.model_dump(mode="json")
    if state is None:
        lifecycle_status = TaskStatus.QUEUED.value
        pipeline_status = PipelineStatus.BACKLOG.value
    else:
        lifecycle_status = state.status.value
        pipeline_status = state.pipeline_status.value
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
        "provenance_json": json.dumps(provenance_payload, sort_keys=True),
        "lifecycle_status": lifecycle_status,
        "pipeline_status": pipeline_status,
    }


def _sync_task_intent_columns(connection: sqlite3.Connection) -> None:
    """
    Backfill the denormalized ``task_intent`` columns from each row's JSON payload.

    Called immediately after migration 7 applies so existing task
    rows have the new columns populated without requiring a
    separate operator step. Skipping a row that won't validate is
    intentional — the migration has already committed, and a single
    bad row shouldn't block the rest from being usable on the new
    schema.
    """
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
    """
    Locate the bundled migration directory via ``importlib.resources``.

    Isolated as a one-liner so tests can monkey-patch this single
    symbol to substitute a fixture set of migrations — patching the
    full ``available_migrations`` would require duplicating the
    discovery logic in every test.
    """
    return importlib.resources.files(MIGRATIONS_PACKAGE)


def available_migrations() -> tuple[Migration, ...]:
    """
    Discover bundled SQL migration files in version order.

    Reads each ``NNNN_name.sql`` from ``litehive.db.migrations`` so
    the apply plan is computed from what's currently shipped, not
    from a Python-side registry that could drift. Called by the
    migration CLI for status output and by
    :func:`apply_pending_migrations` to compute the pending list.
    """
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
    """
    Open a SQLite connection with the project's pragmas applied.

    Single point that toggles foreign keys, sets row factory, and
    applies the test-mode durability shortcuts. Migration code and
    :func:`connect_workspace_db` both call this so the two paths
    cannot diverge on, say, ``foreign_keys`` and let one path
    write rows the other rejects.
    """
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
    """
    Bootstrap the ``schema_migrations`` bookkeeping table on a fresh db.

    Without this, the very first migration-status query would fail
    because the table it reads doesn't yet exist (migration 1 is
    what creates the rest of the schema, but it can't bootstrap
    its own log). Idempotent ``CREATE TABLE IF NOT EXISTS`` so it
    is safe to call before every operation.
    """
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
    """
    Return the set of applied migration versions.

    Used by :func:`migration_status` for status reporting and by
    :func:`apply_pending_migrations` to skip migrations whose
    version is already in the log. The set form is what the apply
    loop actually uses; ordering is preserved by the SQL but
    discarded here.
    """
    _ensure_schema_migrations_table(connection)
    rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return {int(row["version"]) for row in rows}


def _applied_migration_rows(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    """
    Return ``(version, name)`` for each applied migration in order.

    :func:`_database_requires_rebuild` consults the *names* (not
    just versions) to detect a renamed migration that would
    otherwise look "already applied" by version number alone —
    such a rename would silently leave the schema in a state the
    bundled migrations no longer describe.
    """
    _ensure_schema_migrations_table(connection)
    rows = connection.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    applied: list[tuple[int, str]] = []
    for row in rows:
        applied.append((int(row["version"]), str(row["name"])))
    return applied


def _has_required_baseline_tables(
    connection: sqlite3.Connection,
    applied_versions: set[int],
) -> bool:
    """
    Confirm every table the applied migrations should have created is present.

    :func:`_database_requires_rebuild` consults this so a
    partially-applied or table-dropped DB triggers a rebuild
    instead of silently limping forward — a missing
    ``runtime_settings`` table after a successful migration 6
    means something has gone very wrong, and continuing would
    just produce ``OperationalError`` on every read.
    """
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
    """
    True when the DB's applied log is a strict prefix of the bundled migrations.

    :func:`_database_requires_rebuild` uses this to detect a
    renamed, reordered, or otherwise-diverged migration history
    that cannot be reconciled by patching forward — once the
    history shape has diverged, continuing would either skip a
    needed migration or apply one out of order.
    """
    if len(applied) > len(available):
        return False
    expected_prefix = [(migration.version, migration.name) for migration in available[: len(applied)]]
    return applied == expected_prefix


def _database_requires_rebuild(db_path: Path, migrations: tuple[Migration, ...]) -> bool:
    """
    Detect a DB whose history has diverged from the bundled migrations.

    Returns ``True`` when the log doesn't prefix-match the bundled
    list, when required tables for already-applied migrations are
    missing, or when the file itself is unreadable as SQLite. In
    all those cases :func:`apply_pending_migrations` will back up
    and rebuild rather than try to patch a broken history forward.
    """
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
    """
    Read-only migration view used by ``litehive db status``.

    Returns the current version plus the applied/pending lists
    without mutating the DB so the CLI can render status from a
    background process without racing the daemon's schema writes.
    """
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
    """
    Bring the workspace DB up to the bundled schema.

    Called explicitly by ``litehive db migrate`` and lazily by
    :func:`connect_workspace_db` on first open per process. Detects
    diverged history and rebuilds the DB after a guarded backup
    rather than trying to patch forward. ``dry_run`` returns the
    plan without writing anything so the operator can review what
    a migration would do.
    """
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
    """
    Identify the DB file by ``(dev, inode)`` for the migration cache key.

    Tracking identity rather than mtime/size means the in-process
    migration cache invalidates only when the file is replaced
    (e.g. by a rebuild), not on every successful write — without
    this, every write would force a redundant migration check on
    the next open and cripple write throughput.
    """
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
    """
    Canonical cache key for ``MIGRATED_DB_PATHS`` / ``REBUILT_DB_PATHS``.

    Resolves the path so two callers entering through different
    cwd-relative paths share one cache slot — without resolution,
    a CLI running from the workspace root and a daemon running
    from elsewhere would each miss the other's cached state and
    re-run migration checks redundantly.
    """
    return str(db_path.resolve())


def consume_rebuilt_database_marker(root: Path) -> bool:
    """
    One-shot signal that a database rebuild happened this process.

    Called by status/recovery output so the operator sees the
    rebuild warning exactly once per process — leaving the marker
    in place would spam the status block on every subsequent
    invocation. Returns ``True`` the first time and ``False``
    afterwards.
    """
    key = _db_cache_key(workspace_path(root, "data.db"))
    if key not in REBUILT_DB_PATHS:
        return False
    REBUILT_DB_PATHS.remove(key)
    return True


@contextmanager
def connect_workspace_db(root: Path, migrate: bool = True) -> Iterator[sqlite3.Connection]:
    """
    The single entry point for opening the workspace's SQLite database.

    Every workspace read or write goes through this context manager
    so pragmas and on-demand migration stay consistent across
    callers — opening sqlite directly would diverge on
    ``foreign_keys`` or skip the lazy migration check.
    ``migrate=False`` is used by the read-only fast status path
    that must not block on a migration run.
    """
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
