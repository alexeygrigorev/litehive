"""Global cross-workspace registry backed by SQLite."""

import atexit
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable, TypeVar

import yaml

from litehive.config.paths import litehive_config_root, litehive_database_path, workspace_id
from litehive.domain.common import utcnow

log = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass
class _RegistryConnectionState:
    connection: sqlite3.Connection
    file_identity: tuple[int, int] | None


_REGISTRY_CONNECTIONS: dict[str, _RegistryConnectionState] = {}
_REGISTRY_CONNECTIONS_MUTEX = threading.RLock()

_DEFAULT_BUSY_TIMEOUT_MS = 30_000
_DEFAULT_LOCK_RETRIES = 1
_DEFAULT_LOCK_RETRY_DELAY_MS = 100

_CREATE_WORKSPACES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    last_seen TEXT NOT NULL
)
"""
_CREATE_WORKSPACES_LAST_SEEN_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS workspaces_last_seen_idx
ON workspaces (last_seen DESC)
"""
_UPSERT_WORKSPACE_SQL = """
INSERT INTO workspaces (workspace_id, path, last_seen)
VALUES (?, ?, ?)
ON CONFLICT(workspace_id) DO UPDATE SET
    path = excluded.path,
    last_seen = excluded.last_seen
"""


def _registry_db_path() -> Path:
    return litehive_database_path().expanduser().resolve()


def _legacy_registry_path() -> Path:
    return (litehive_config_root() / "workspaces").with_suffix(".yaml")


def _db_file_identity(db_path: Path) -> tuple[int, int] | None:
    try:
        stat = db_path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


def _close_connection(connection: sqlite3.Connection) -> None:
    try:
        connection.close()
    except sqlite3.Error:
        pass


def _discard_cached_connection_unlocked(key: str) -> None:
    state = _REGISTRY_CONNECTIONS.pop(key, None)
    if state is not None:
        _close_connection(state.connection)


def _close_all_registry_connections() -> None:
    with _REGISTRY_CONNECTIONS_MUTEX:
        while _REGISTRY_CONNECTIONS:
            _, state = _REGISTRY_CONNECTIONS.popitem()
            _close_connection(state.connection)


atexit.register(_close_all_registry_connections)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def _registry_busy_timeout_ms() -> int:
    return max(_int_env("LITEHIVE_REGISTRY_BUSY_TIMEOUT_MS", _DEFAULT_BUSY_TIMEOUT_MS), 1)


def _registry_lock_retries() -> int:
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRIES", _DEFAULT_LOCK_RETRIES)


def _registry_lock_retry_delay_seconds() -> float:
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRY_DELAY_MS", _DEFAULT_LOCK_RETRY_DELAY_MS) / 1000


def _open_registry_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_ms = _registry_busy_timeout_ms()
    connection = sqlite3.connect(db_path, timeout=timeout_ms / 1000, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    connection.execute("PRAGMA journal_mode = WAL")
    if os.environ.get("LITEHIVE_SKIP_FSYNC"):
        connection.execute("PRAGMA synchronous = OFF")
    else:
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def _connection_is_healthy(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("PRAGMA schema_version").fetchone()
    except sqlite3.Error:
        return False
    return True


def _connection_for_registry() -> tuple[str, Path, sqlite3.Connection]:
    db_path = _registry_db_path()
    key = str(db_path)
    state = _REGISTRY_CONNECTIONS.get(key)
    if (
        state is not None
        and state.file_identity == _db_file_identity(db_path)
        and _connection_is_healthy(state.connection)
    ):
        return key, db_path, state.connection
    if state is not None:
        _discard_cached_connection_unlocked(key)
    connection = _open_registry_connection(db_path)
    _REGISTRY_CONNECTIONS[key] = _RegistryConnectionState(
        connection=connection,
        file_identity=_db_file_identity(db_path),
    )
    return key, db_path, connection


def _ensure_registry_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_CREATE_WORKSPACES_TABLE_SQL)
    connection.execute(_CREATE_WORKSPACES_LAST_SEEN_INDEX_SQL)


def _load_legacy_registry_entries(path: Path) -> list[Path]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError) as exc:
        log.warning("legacy workspace registry %s is unreadable (%s); removing it", path, exc)
        return []
    if not isinstance(payload, list):
        log.warning("legacy workspace registry %s must contain a list of workspace paths; removing it", path)
        return []

    roots: list[Path] = []
    seen: set[Path] = set()
    for entry in payload:
        if not isinstance(entry, str):
            continue
        try:
            resolved = Path(entry).expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def _migrate_legacy_registry(connection: sqlite3.Connection) -> None:
    legacy_path = _legacy_registry_path()
    if not legacy_path.exists():
        return

    entries = _load_legacy_registry_entries(legacy_path)
    if entries:
        now = utcnow()
        with connection:
            connection.executemany(
                _UPSERT_WORKSPACE_SQL,
                [(workspace_id(root), str(root), now) for root in entries],
            )
    try:
        legacy_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("failed to remove legacy workspace registry %s (%s)", legacy_path, exc)


def _rebuild_registry_db(db_path: Path, key: str, exc: Exception) -> None:
    log.warning("workspace registry database %s is unreadable (%s); rebuilding", db_path, exc)
    with _REGISTRY_CONNECTIONS_MUTEX:
        _discard_cached_connection_unlocked(key)
    try:
        db_path.unlink(missing_ok=True)
    except OSError as unlink_exc:
        log.warning("failed to delete broken workspace registry database %s (%s)", db_path, unlink_exc)


def _reset_registry_connection(key: str) -> None:
    with _REGISTRY_CONNECTIONS_MUTEX:
        _discard_cached_connection_unlocked(key)


def _is_lock_contention_error(exc: sqlite3.DatabaseError) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _run_registry_operation(
    operation: Callable[[sqlite3.Connection], _T],
    *,
    default: _T,
) -> _T:
    db_path = _registry_db_path()
    key = str(db_path)
    last_error: Exception | None = None
    rebuild_attempted = False
    remaining_lock_retries = _registry_lock_retries()
    while True:
        try:
            with _REGISTRY_CONNECTIONS_MUTEX:
                key, db_path, connection = _connection_for_registry()
                _ensure_registry_schema(connection)
                _migrate_legacy_registry(connection)
                return operation(connection)
        except sqlite3.DatabaseError as exc:
            last_error = exc
            if _is_lock_contention_error(exc):
                _reset_registry_connection(key)
                if remaining_lock_retries <= 0:
                    log.warning(
                        "workspace registry database %s remained locked after retry (%s)",
                        db_path,
                        exc,
                    )
                    return default
                remaining_lock_retries -= 1
                time.sleep(_registry_lock_retry_delay_seconds())
                continue
            if rebuild_attempted:
                break
            rebuild_attempted = True
            _rebuild_registry_db(db_path, key, exc)
        except OSError as exc:
            log.warning("workspace registry database %s is unavailable (%s)", db_path, exc)
            return default
    if last_error is not None:
        log.warning("workspace registry database %s remains unavailable after rebuild (%s)", db_path, last_error)
    return default


def list_registered_workspace_paths() -> list[Path]:
    def _list(connection: sqlite3.Connection) -> list[Path]:
        rows = connection.execute(
            "SELECT path FROM workspaces ORDER BY last_seen DESC, path ASC"
        ).fetchall()
        roots: list[Path] = []
        seen: set[Path] = set()
        for row in rows:
            try:
                root = Path(str(row["path"])).expanduser().resolve()
            except OSError:
                continue
            if root in seen:
                continue
            seen.add(root)
            roots.append(root)
        return roots

    return _run_registry_operation(_list, default=[])


def register_workspace_path(root: Path) -> None:
    resolved = root.expanduser().resolve()
    now = utcnow()

    def _register(connection: sqlite3.Connection) -> None:
        with connection:
            connection.execute(
                _UPSERT_WORKSPACE_SQL,
                (workspace_id(resolved), str(resolved), now),
            )
        return None

    _run_registry_operation(_register, default=None)
