"""Global cross-workspace registry backed by SQLite."""

from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import sqlite3
import threading

from litehive.config.paths import litehive_database_path, workspace_id

log = logging.getLogger(__name__)

_REGISTRY_CONNECTIONS: dict[str, sqlite3.Connection] = {}


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connection_key(db_path: Path) -> str:
    return f"{db_path.expanduser().resolve()}::{os.getpid()}::{threading.get_ident()}"


def _close_cached_connections(db_path: Path) -> None:
    prefix = f"{db_path.expanduser().resolve()}::"
    stale_keys = [key for key in _REGISTRY_CONNECTIONS if key.startswith(prefix)]
    for key in stale_keys:
        connection = _REGISTRY_CONNECTIONS.pop(key, None)
        if connection is None:
            continue
        connection.close()


def _open_connection(db_path: Path) -> sqlite3.Connection:
    key = _connection_key(db_path)
    cached = _REGISTRY_CONNECTIONS.get(key)
    if cached is not None:
        return cached

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    if os.environ.get("LITEHIVE_SKIP_FSYNC"):
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA temp_store = MEMORY")
    _REGISTRY_CONNECTIONS[key] = connection
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workspaces (
            workspace_id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            last_seen TEXT NOT NULL
        )
        """
    )
    connection.commit()


def _upsert_workspace(connection: sqlite3.Connection, root: Path) -> None:
    resolved = root.expanduser().resolve()
    connection.execute(
        """
        INSERT INTO workspaces (workspace_id, path, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(workspace_id) DO UPDATE
        SET path = excluded.path,
            last_seen = excluded.last_seen
        """,
        (workspace_id(resolved), str(resolved), _utcnow()),
    )
    connection.commit()


def _rebuild_registry_db(db_path: Path, exc: Exception) -> None:
    log.warning("workspace registry database %s was unusable (%s); rebuilding", db_path, exc)
    _close_cached_connections(db_path)
    for candidate in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            pass


def _with_registry(operation):
    db_path = litehive_database_path()
    for attempt in range(2):
        try:
            connection = _open_connection(db_path)
            _ensure_schema(connection)
            return operation(connection)
        except sqlite3.DatabaseError as exc:
            if attempt == 1:
                raise
            _rebuild_registry_db(db_path, exc)
    raise RuntimeError("unreachable")


def list_registered_workspace_paths() -> list[Path]:
    try:
        rows = _with_registry(
            lambda connection: connection.execute(
                "SELECT path FROM workspaces ORDER BY last_seen DESC, path ASC"
            ).fetchall()
        )
    except sqlite3.DatabaseError as exc:
        log.warning("workspace registry database %s remained unreadable (%s); continuing empty", litehive_database_path(), exc)
        return []
    roots: list[Path] = []
    seen: set[Path] = set()
    for row in rows:
        resolved = Path(str(row["path"])).expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def register_workspace_path(root: Path) -> None:
    try:
        _with_registry(lambda connection: _upsert_workspace(connection, root))
    except sqlite3.DatabaseError as exc:
        log.warning("workspace registry database %s remained unwritable (%s); skipping", litehive_database_path(), exc)
