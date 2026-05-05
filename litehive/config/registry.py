"""Global cross-workspace registry backed by SQLite."""

from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time

from litehive.config.paths import litehive_root

log = logging.getLogger(__name__)

_REGISTRY_MUTEX = threading.RLock()

_DEFAULT_BUSY_TIMEOUT_MS = 30_000
_DEFAULT_LOCK_RETRIES = 0
_DEFAULT_LOCK_RETRY_DELAY_MS = 100
_REGISTRY_TABLE = "workspace_registry"


class WorkspaceRegistryError(RuntimeError):
    """Raised when the global workspace registry cannot be read or written."""


def workspace_registry_path() -> Path:
    """Resolve the cross-workspace SQLite registry file under the user's litehive root; this is the single shared file that lets ``litehive status`` and discovery list every workspace the operator has ever initialized on this machine."""
    return litehive_root() / "workspaces.db"


def _int_env(name: str, default: int) -> int:
    """Read a non-negative integer from the named env var, falling back to ``default`` for unset or unparseable values; used by the registry tunables (lock retries, busy timeout) so operators can override locking behaviour without editing code."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def _registry_lock_retries() -> int:
    """Number of extra ``database is locked`` retries the registry tolerates beyond the SQLite busy timeout; consumed by ``_locked_registry_operation`` and tunable via env so CI can crank it up without code changes."""
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRIES", _DEFAULT_LOCK_RETRIES)


def _registry_busy_timeout_ms() -> int:
    """SQLite ``PRAGMA busy_timeout`` value in milliseconds, clamped to at least 1ms; used by the connection opener to bound how long a writer waits on a contended registry."""
    return max(_int_env("LITEHIVE_REGISTRY_BUSY_TIMEOUT_MS", _DEFAULT_BUSY_TIMEOUT_MS), 1)


def _registry_busy_timeout_seconds() -> float:
    """Same busy timeout as :func:`_registry_busy_timeout_ms` but in seconds, for the ``sqlite3.connect(timeout=…)`` parameter which uses seconds rather than milliseconds."""
    return _registry_busy_timeout_ms() / 1000


def _registry_lock_retry_delay_seconds() -> float:
    """Sleep duration between ``_locked_registry_operation`` retry attempts; small by default so the worst case is a brief stall rather than a fast spin."""
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRY_DELAY_MS", _DEFAULT_LOCK_RETRY_DELAY_MS) / 1000


def _open_registry_connection(path: Path) -> sqlite3.Connection:
    """Open an autocommit sqlite connection to the registry with WAL (or in-memory journals when ``LITEHIVE_SKIP_FSYNC`` is set for tests), creating the parent directory if needed; the single chokepoint that all registry reads/writes go through so pragmas stay consistent."""
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
    """Create the registry table and its ``registered_at`` index on first contact; called by every read/write helper so a freshly-quarantined or never-existed registry rebuilds itself transparently on the next access."""
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
    """Run ``PRAGMA quick_check`` and raise ``DatabaseError`` if SQLite reports anything other than ``ok``; called by :func:`workspace_registry_error` so corruption can be surfaced through a structured exception path rather than silent breakage."""
    row = connection.execute("PRAGMA quick_check").fetchone()
    if row is None:
        return
    result = str(row[0])
    if result.lower() != "ok":
        raise sqlite3.DatabaseError(result)


def workspace_registry_error() -> str | None:
    """Run ``PRAGMA quick_check`` on the registry and return a short error string when the file is corrupt, or None when it is healthy or absent; consumed by status diagnostics to surface "registry broken" without taking the registry lock for a real query."""
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
    """Move a corrupt registry file aside under a timestamped ``.corrupt-*`` name and return the new path; called inline by the read/write helpers when SQLite raises ``DatabaseError`` so the next operation can recreate a fresh registry instead of failing forever."""
    path = workspace_registry_path()
    return _backup_corrupt_registry_file(path, reason=reason, label="workspace registry")


def _backup_corrupt_registry_file(path: Path, reason: str, label: str) -> Path | None:
    """Atomically rename a corrupt sqlite file to a ``.corrupt-<UTC timestamp>`` sibling and log the reason; the single rename helper used by :func:`quarantine_corrupt_workspace_registry` so the on-disk artifact format is consistent across registry types."""
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


def _canonical_workspace_root(root: Path | str) -> Path | None:
    """Expand ``~`` and resolve symlinks so two registry rows pointing at the same physical directory deduplicate to one; returns None on filesystem errors so a single broken row cannot poison the whole listing."""
    try:
        return Path(root).expanduser().resolve()
    except OSError:
        return None


def _locked_registry_operation(operation, path: Path):
    """Run a registry callable, retrying through ``database is locked``/``busy`` errors up to ``_registry_lock_retries`` times before surfacing a ``TimeoutError``; wraps every public registry read/write so contention with another process becomes a bounded wait, not an immediate failure."""
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
    """SELECT all workspace roots newest-first and canonicalize each one, dropping unresolvable or duplicate entries; the inner SQL+canonicalization step that :func:`list_registered_workspace_paths` wraps with locking and self-healing."""
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
    """Return every canonical workspace root the registry knows about, newest-first, after self-healing one round of corruption; called by workspace discovery (``litehive status``, multi-workspace dashboards) which must enumerate every workspace this user has ever initialized."""
    path = workspace_registry_path()
    with _REGISTRY_MUTEX:
        for attempt in range(2):
            try:
                return _locked_registry_operation(
                    lambda: _list_registered_workspace_paths(path),
                    path=path,
                )
            except TimeoutError as exc:
                raise WorkspaceRegistryError(str(exc)) from exc
            except sqlite3.DatabaseError as exc:
                if attempt == 0 and quarantine_corrupt_workspace_registry(str(exc)) is not None:
                    continue
                raise WorkspaceRegistryError(f"failed to read workspace registry {path}: {exc}") from exc
            except OSError as exc:
                raise WorkspaceRegistryError(f"failed to read workspace registry {path}: {exc}") from exc
    raise WorkspaceRegistryError(f"failed to read workspace registry {path}")


def _list_registered_workspace_paths(path: Path) -> list[Path]:
    """Open a connection, ensure schema, and read all registered roots; the lock-protected callable that :func:`list_registered_workspace_paths` hands to ``_locked_registry_operation``."""
    with _open_registry_connection(path) as connection:
        _ensure_registry_schema(connection)
        return _read_registered_workspace_paths(connection)


def register_workspace_path(root: Path) -> None:
    """Upsert a workspace root into the registry with the current timestamp, silently dropping unresolvable paths; called once during workspace bootstrap so future cross-workspace discovery can find this root without scanning the filesystem."""
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
                raise WorkspaceRegistryError(str(exc)) from exc
            except sqlite3.DatabaseError as exc:
                if attempt == 0 and quarantine_corrupt_workspace_registry(str(exc)) is not None:
                    continue
                raise WorkspaceRegistryError(f"failed to update workspace registry {path}: {exc}") from exc
            except OSError as exc:
                raise WorkspaceRegistryError(f"failed to update workspace registry {path}: {exc}") from exc


def _register_workspace_path(path: Path, root: Path) -> None:
    """Upsert one workspace root inside an immediate transaction so concurrent registrations from sibling workspaces serialize cleanly; the lock-protected callable :func:`register_workspace_path` hands to ``_locked_registry_operation``."""
    with _open_registry_connection(path) as connection:
        _ensure_registry_schema(connection)
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
