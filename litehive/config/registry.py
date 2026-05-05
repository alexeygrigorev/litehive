"""
Global cross-workspace registry backed by SQLite.

Lives outside any one workspace so a single shared file remembers
every workspace this user has ever initialized on this machine.
``litehive status`` and multi-workspace dashboards rely on this to
enumerate workspaces without filesystem scanning. Corruption is
recoverable: a quick-check failure quarantines the file and the
next operation rebuilds an empty registry.
"""

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
    """
    Raised when the global workspace registry cannot be read or written.

    Distinct from generic ``OSError`` / ``sqlite3.DatabaseError``
    so callers can catch only registry-level failures (timeouts,
    corruption that survived self-heal) without masking other
    SQLite errors that come from a workspace's own database.
    """


def workspace_registry_path() -> Path:
    """
    Resolve the cross-workspace SQLite registry file path.

    Lives under the user's litehive root so a single shared file
    spans every workspace they have ever initialized; without that
    shared file, multi-workspace discovery would have to scan the
    filesystem.
    """
    return litehive_root() / "workspaces.db"


def _int_env(name: str, default: int) -> int:
    """
    Read a non-negative integer from the named env var.

    Falls back to ``default`` for unset or unparseable values.
    Used by the registry tunables (lock retries, busy timeout) so
    operators and CI can override locking behaviour via env without
    editing code.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def _registry_lock_retries() -> int:
    """
    Extra retries the registry tolerates beyond the SQLite busy timeout.

    Consumed by :func:`_locked_registry_operation`. Tunable via
    ``LITEHIVE_REGISTRY_LOCK_RETRIES`` so CI can crank it up
    without code changes when concurrent test runs hit the same
    file.
    """
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRIES", _DEFAULT_LOCK_RETRIES)


def _registry_busy_timeout_ms() -> int:
    """
    SQLite ``PRAGMA busy_timeout`` value in milliseconds.

    Clamped to at least 1 ms so a misconfigured zero does not
    disable busy waits entirely. Used by the connection opener to
    bound how long a writer waits on a contended registry before
    surfacing a "locked" error to the retry loop.
    """
    return max(_int_env("LITEHIVE_REGISTRY_BUSY_TIMEOUT_MS", _DEFAULT_BUSY_TIMEOUT_MS), 1)


def _registry_busy_timeout_seconds() -> float:
    """
    Busy timeout in seconds for the ``sqlite3.connect`` ``timeout`` parameter.

    Same value as :func:`_registry_busy_timeout_ms`, just rescaled.
    The connect-level timeout uses seconds while the pragma uses
    milliseconds; keeping both in sync prevents one bound from
    silently overriding the other.
    """
    return _registry_busy_timeout_ms() / 1000


def _registry_lock_retry_delay_seconds() -> float:
    """
    Sleep between :func:`_locked_registry_operation` retry attempts.

    Small by default so the worst case is a brief stall rather
    than a tight CPU spin. Tunable via env so a test that wants
    to drive contention can pin the delay to zero.
    """
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRY_DELAY_MS", _DEFAULT_LOCK_RETRY_DELAY_MS) / 1000


def _open_registry_connection(path: Path) -> sqlite3.Connection:
    """
    Open an autocommit SQLite connection to the registry.

    Creates the parent directory if needed and applies WAL
    journaling — except under ``LITEHIVE_SKIP_FSYNC`` (tests)
    where in-memory journals trade durability for speed. The
    single chokepoint every registry read/write goes through so
    pragmas stay consistent.
    """
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
    """
    Create the registry table and its index on first contact.

    Called by every read/write helper so a freshly-quarantined or
    never-existed registry rebuilds itself transparently on the
    next access; the alternative would be a separate bootstrap
    step that callers might forget.
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


def _registry_quick_check(connection: sqlite3.Connection) -> None:
    """
    Run ``PRAGMA quick_check`` and raise on a non-``ok`` result.

    Called by :func:`workspace_registry_error` so corruption is
    surfaced through a structured exception path rather than
    silent breakage; status diagnostics use that path to render
    the "registry broken" notice without taking the registry
    write lock.
    """
    row = connection.execute("PRAGMA quick_check").fetchone()
    if row is None:
        return
    result = str(row[0])
    if result.lower() != "ok":
        raise sqlite3.DatabaseError(result)


def workspace_registry_error() -> str | None:
    """
    Probe the registry for corruption and return a short error label.

    Returns ``None`` when the registry is healthy or absent.
    Consumed by status diagnostics to surface "registry broken"
    without taking the registry write lock; the cheap
    ``PRAGMA quick_check`` is enough to flag a corrupt file
    before a more expensive operation hits the same problem.
    """
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
    """
    Move a corrupt registry file aside under a timestamped name.

    Called inline by the read/write helpers when SQLite raises
    ``DatabaseError`` so the next operation can recreate a fresh
    registry instead of failing forever. The quarantined file is
    preserved (with the failure reason in a log line) so an
    operator can inspect it after the fact.
    """
    path = workspace_registry_path()
    return _backup_corrupt_registry_file(path, reason=reason, label="workspace registry")


def _backup_corrupt_registry_file(path: Path, reason: str, label: str) -> Path | None:
    """
    Atomically rename a corrupt sqlite file to a timestamped sibling.

    The single rename helper used by
    :func:`quarantine_corrupt_workspace_registry` so the on-disk
    artifact format (``<name>.corrupt-<UTC timestamp>``) stays
    consistent across registry types — important when an operator
    later cleans up after the fact.
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


def _canonical_workspace_root(root: Path | str) -> Path | None:
    """
    Canonicalize a workspace root for stable equality.

    Expands ``~`` and resolves symlinks so two registry rows
    pointing at the same physical directory deduplicate to one
    entry on read. Returns ``None`` on filesystem errors so a
    single broken row cannot poison the whole listing.
    """
    try:
        return Path(root).expanduser().resolve()
    except OSError:
        return None


def _locked_registry_operation(operation, path: Path):
    """
    Run a registry callable with bounded retries through SQLite locks.

    Retries through ``database is locked``/``busy`` errors up to
    :func:`_registry_lock_retries` times before surfacing
    ``TimeoutError``; wraps every public registry read/write so
    contention with another process becomes a bounded wait rather
    than an immediate failure visible to the operator.
    """
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
    """
    Read all workspace roots newest-first, canonicalized and deduplicated.

    The inner SQL+canonicalization step that
    :func:`list_registered_workspace_paths` wraps with locking
    and self-healing. Drops unresolvable rows so one broken entry
    does not break the listing for the rest of the workspaces.
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
        resolved = _canonical_workspace_root(row["root"])
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def list_registered_workspace_paths() -> list[Path]:
    """
    Return every canonical workspace root the registry knows about.

    Newest-first, after self-healing one round of corruption (a
    fresh registry is rebuilt automatically on the second
    attempt). Called by workspace discovery — ``litehive status``,
    multi-workspace dashboards — which must enumerate every
    workspace this user has initialized without filesystem
    scanning.
    """
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
    """
    Open a registry connection, ensure schema, and read all roots.

    The lock-protected callable that
    :func:`list_registered_workspace_paths` hands to
    :func:`_locked_registry_operation`; kept private and
    parameterless except for ``path`` so the retry wrapper does
    not have to know about schema bootstrap.
    """
    with _open_registry_connection(path) as connection:
        _ensure_registry_schema(connection)
        return _read_registered_workspace_paths(connection)


def register_workspace_path(root: Path) -> None:
    """
    Upsert a workspace root with the current timestamp.

    Silently drops unresolvable paths so a transient filesystem
    error during bootstrap does not block workspace creation.
    Called once during ``ensure_workspace`` so future
    cross-workspace discovery can find this root without scanning
    the filesystem; also called when discovery resolves a
    workspace via cwd or env so registration stays current.
    """
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
    """
    Upsert one workspace root inside an immediate transaction.

    Concurrent registrations from sibling workspaces serialize
    cleanly via ``BEGIN IMMEDIATE`` rather than racing through
    autocommit; the alternative would lose timestamp updates when
    two CLI calls land at the same instant. Lock-protected
    callable handed to :func:`_locked_registry_operation`.
    """
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
