"""
SQLite storage helpers for the workspace registry.

``config.registry`` owns the public service and dependency-injection
boundary. This module owns the registry file's SQLite schema,
connections, row queries, corruption quarantine, and workspace-root
canonicalization.
"""

from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import sqlite3

from litehive.config.registry_locking import registry_busy_timeout_ms, registry_busy_timeout_seconds

log = logging.getLogger(__name__)

_REGISTRY_TABLE = "workspace_registry"


def open_registry_connection(path: Path) -> sqlite3.Connection:
    """
    Open an autocommit SQLite connection to the registry.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=registry_busy_timeout_seconds(), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {registry_busy_timeout_ms()}")
    if os.environ.get("LITEHIVE_SKIP_FSYNC"):
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA journal_mode = MEMORY")
        connection.execute("PRAGMA temp_store = MEMORY")
    else:
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


def ensure_registry_schema(connection: sqlite3.Connection) -> None:
    """
    Create the registry table and its index on first contact.
    """
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_REGISTRY_TABLE} (
            root TEXT PRIMARY KEY,
            registered_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{_REGISTRY_TABLE}_registered_at
        ON {_REGISTRY_TABLE} (registered_at DESC, root DESC)
        """
    )


def registry_quick_check(connection: sqlite3.Connection) -> None:
    """
    Run ``PRAGMA quick_check`` and raise on a non-``ok`` result.
    """
    row = connection.execute("PRAGMA quick_check").fetchone()
    if row is None:
        return
    result = str(row[0])
    if result.lower() != "ok":
        raise sqlite3.DatabaseError(result)


def backup_corrupt_registry_file(path: Path, reason: str, label: str) -> Path | None:
    """
    Atomically rename a corrupt sqlite file to a timestamped sibling.
    """
    if not path.exists():
        return None
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.corrupt-{timestamp}")
    try:
        path.replace(backup)
    except OSError as exc:
        log.warning("failed to move corrupt %s %s aside (%s)", label, path, exc)
        return None
    log.warning("%s %s was corrupt (%s); moved it to %s", label, path, reason, backup)
    return backup


def canonical_workspace_root(root: Path | str) -> Path | None:
    """
    Canonicalize a workspace root for stable registry equality.
    """
    try:
        return Path(root).expanduser().resolve()
    except OSError:
        return None


def read_registered_workspace_paths(connection: sqlite3.Connection) -> list[Path]:
    """
    Read all workspace roots newest-first, canonicalized and deduplicated.
    """
    rows = connection.execute(
        f"""
        SELECT root
        FROM {_REGISTRY_TABLE}
        ORDER BY registered_at DESC, root DESC
        """
    ).fetchall()
    roots: list[Path] = []
    seen: set[Path] = set()
    for row in rows:
        resolved = canonical_workspace_root(row["root"])
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def list_registered_workspace_paths_from_store(path: Path) -> list[Path]:
    """
    Open the registry store, ensure schema, and read all roots.
    """
    with open_registry_connection(path) as connection:
        ensure_registry_schema(connection)
        return read_registered_workspace_paths(connection)


def register_workspace_path_in_store(path: Path, root: Path) -> None:
    """
    Upsert one workspace root inside an immediate transaction.
    """
    with open_registry_connection(path) as connection:
        ensure_registry_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                f"""
                INSERT INTO {_REGISTRY_TABLE} (root, registered_at)
                VALUES (?, ?)
                ON CONFLICT(root) DO UPDATE SET registered_at = excluded.registered_at
                """,
                (str(root), datetime.now(UTC).isoformat().replace("+00:00", "Z")),
            )
            connection.commit()
        except sqlite3.DatabaseError:
            connection.rollback()
            raise
