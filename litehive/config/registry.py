"""Global cross-workspace registry backed by SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time

import yaml

from litehive.config.paths import litehive_config_root, litehive_root

log = logging.getLogger(__name__)

_REGISTRY_MUTEX = threading.RLock()

_DEFAULT_BUSY_TIMEOUT_MS = 30_000
_DEFAULT_LOCK_RETRIES = 0
_DEFAULT_LOCK_RETRY_DELAY_MS = 100
_REGISTRY_TABLE = "workspace_registry"


class _LegacyRegistryCorruptError(RuntimeError):
    """Raised when the legacy YAML registry cannot be migrated safely."""


def workspace_registry_path() -> Path:
    return litehive_root() / "workspaces.db"


def legacy_workspace_registry_path() -> Path:
    return litehive_config_root() / "workspaces.yaml"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def _registry_lock_retries() -> int:
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRIES", _DEFAULT_LOCK_RETRIES)


def _registry_busy_timeout_ms() -> int:
    return max(_int_env("LITEHIVE_REGISTRY_BUSY_TIMEOUT_MS", _DEFAULT_BUSY_TIMEOUT_MS), 1)


def _registry_busy_timeout_seconds() -> float:
    return _registry_busy_timeout_ms() / 1000


def _registry_lock_retry_delay_seconds() -> float:
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRY_DELAY_MS", _DEFAULT_LOCK_RETRY_DELAY_MS) / 1000


def _open_registry_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=_registry_busy_timeout_seconds(), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {_registry_busy_timeout_ms()}")
    if os.environ.get("LITEHIVE_SKIP_FSYNC"):
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA journal_mode = MEMORY")
        connection.execute("PRAGMA temp_store = MEMORY")
    else:
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _ensure_registry_schema(connection: sqlite3.Connection) -> None:
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


def _registry_quick_check(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA quick_check").fetchone()
    if row is None:
        return
    result = str(row[0])
    if result.lower() != "ok":
        raise sqlite3.DatabaseError(result)


def workspace_registry_error() -> str | None:
    path = workspace_registry_path()
    if not path.exists():
        return None
    try:
        with _open_registry_connection(path) as connection:
            _registry_quick_check(connection)
    except (OSError, sqlite3.DatabaseError) as exc:
        return str(exc)
    return None


def quarantine_corrupt_workspace_registry(reason: str) -> Path | None:
    path = workspace_registry_path()
    return _backup_corrupt_registry_file(path, reason=reason, label="workspace registry")


def _backup_corrupt_registry_file(path: Path, *, reason: str, label: str) -> Path | None:
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


def _legacy_registry_entries(path: Path) -> list[Path]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError as exc:
        raise _LegacyRegistryCorruptError(str(exc)) from exc
    except OSError as exc:
        raise OSError(f"failed to read legacy workspace registry {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise _LegacyRegistryCorruptError("legacy workspace registry must contain a list of workspace paths")
    if any(not isinstance(entry, str) for entry in payload):
        raise _LegacyRegistryCorruptError("legacy workspace registry must contain only string workspace paths")

    roots: list[Path] = []
    seen: set[Path] = set()
    for entry in payload:
        resolved = _canonical_workspace_root(entry)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def _canonical_workspace_root(root: Path | str) -> Path | None:
    try:
        return Path(root).expanduser().resolve()
    except OSError:
        return None


def _migration_timestamps(count: int) -> list[str]:
    if count <= 0:
        return []
    now = datetime.now(UTC)
    return [
        (now - timedelta(microseconds=index)).isoformat().replace("+00:00", "Z")
        for index in range(count)
    ]


def _migrate_legacy_registry_if_needed(connection: sqlite3.Connection) -> None:
    legacy_path = legacy_workspace_registry_path()
    if not legacy_path.exists():
        return
    try:
        roots = _legacy_registry_entries(legacy_path)
    except _LegacyRegistryCorruptError as exc:
        _backup_corrupt_registry_file(
            legacy_path,
            reason=str(exc),
            label="legacy workspace registry",
        )
        return
    except OSError as exc:
        log.warning("%s", exc)
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        _ensure_registry_schema(connection)
        for timestamp, root in zip(_migration_timestamps(len(roots)), roots, strict=False):
            connection.execute(
                f"""
                INSERT INTO {_REGISTRY_TABLE} (root, registered_at)
                VALUES (?, ?)
                ON CONFLICT(root) DO UPDATE SET
                    registered_at = CASE
                        WHEN excluded.registered_at > {_REGISTRY_TABLE}.registered_at
                        THEN excluded.registered_at
                        ELSE {_REGISTRY_TABLE}.registered_at
                    END
                """,
                (str(root), timestamp),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    try:
        legacy_path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("failed to remove legacy workspace registry %s after migration (%s)", legacy_path, exc)


def _locked_registry_operation(operation, *, path: Path):
    retries_remaining = _registry_lock_retries()
    retry_delay_seconds = _registry_lock_retry_delay_seconds()
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if retries_remaining <= 0:
                raise TimeoutError(f"workspace registry remained locked: {path}") from None
            retries_remaining -= 1
            if retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)


def _read_registered_workspace_paths(connection: sqlite3.Connection) -> list[Path]:
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
        resolved = _canonical_workspace_root(row["root"])
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def list_registered_workspace_paths() -> list[Path]:
    path = workspace_registry_path()
    with _REGISTRY_MUTEX:
        for attempt in range(2):
            try:
                return _locked_registry_operation(
                    lambda: _list_registered_workspace_paths(path),
                    path=path,
                )
            except TimeoutError as exc:
                log.warning("%s", exc)
                return []
            except sqlite3.DatabaseError as exc:
                if attempt == 0 and quarantine_corrupt_workspace_registry(str(exc)) is not None:
                    continue
                log.warning("failed to read workspace registry %s (%s)", path, exc)
                return []
            except OSError as exc:
                log.warning("failed to read workspace registry %s (%s)", path, exc)
                return []
    return []


def _list_registered_workspace_paths(path: Path) -> list[Path]:
    with _open_registry_connection(path) as connection:
        _ensure_registry_schema(connection)
        _migrate_legacy_registry_if_needed(connection)
        return _read_registered_workspace_paths(connection)


def register_workspace_path(root: Path) -> None:
    resolved = _canonical_workspace_root(root)
    if resolved is None:
        return
    path = workspace_registry_path()
    with _REGISTRY_MUTEX:
        for attempt in range(2):
            try:
                _locked_registry_operation(
                    lambda: _register_workspace_path(path, resolved),
                    path=path,
                )
                return
            except TimeoutError as exc:
                log.warning("%s", exc)
                return
            except sqlite3.DatabaseError as exc:
                if attempt == 0 and quarantine_corrupt_workspace_registry(str(exc)) is not None:
                    continue
                log.warning("failed to update workspace registry %s (%s)", path, exc)
                return
            except OSError as exc:
                log.warning("failed to update workspace registry %s (%s)", path, exc)
                return


def _register_workspace_path(path: Path, root: Path) -> None:
    with _open_registry_connection(path) as connection:
        _ensure_registry_schema(connection)
        _migrate_legacy_registry_if_needed(connection)
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
        except Exception:
            connection.rollback()
            raise
