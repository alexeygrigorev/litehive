"""
Global cross-workspace registry backed by SQLite.

Lives outside any one workspace so a single shared file remembers
every workspace this user has ever initialized on this machine.
``litehive status`` and multi-workspace dashboards rely on this to
enumerate workspaces without filesystem scanning. Corruption is
recoverable: a quick-check failure quarantines the file and the
next operation rebuilds an empty registry.

Registry writes have three separate contention controls. The
module-level mutex serializes threads inside one Python process so
two CLI helpers do not race each other through the same connection
setup. SQLite ``busy_timeout`` lets a process wait for another
process's write transaction to finish. The optional retry loop sits
outside SQLite and is only for tests or operators that deliberately
want extra attempts after SQLite has already returned ``locked`` or
``busy``.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading

from litehive.config.paths import litehive_root
from litehive.config.registry_locking import locked_registry_operation
from litehive.config.registry_store import (
    backup_corrupt_registry_file,
    canonical_workspace_root,
    list_registered_workspace_paths_from_store,
    open_registry_connection,
    register_workspace_path_in_store,
    registry_quick_check,
)

_REGISTRY_MUTEX = threading.RLock()


class WorkspaceRegistryError(RuntimeError):
    """
    Raised when the global workspace registry cannot be read or written.

    Distinct from generic ``OSError`` / ``sqlite3.DatabaseError``
    so callers can catch only registry-level failures (timeouts,
    corruption that survived self-heal) without masking other
    SQLite errors that come from a workspace's own database.
    """


@dataclass(frozen=True)
class WorkspaceRegistry:
    """
    Bound access to one workspace-registry SQLite file.

    Public module functions build the default instance for CLI
    boundaries, while tests and future containers can inject a
    registry with an explicit path or mutex instead of relying on
    module-level state.
    """

    path: Path
    mutex: AbstractContextManager[object]

    def error(self) -> str | None:
        """
        Probe this registry for corruption and return a short error label.
        """
        if not self.path.exists():
            return None
        try:
            with open_registry_connection(self.path) as connection:
                registry_quick_check(connection)
        except (OSError, sqlite3.DatabaseError) as exc:
            return str(exc)
        return None

    def quarantine_corrupt(self, reason: str) -> Path | None:
        """
        Move this registry file aside after a corruption failure.
        """
        return backup_corrupt_registry_file(self.path, reason=reason, label="workspace registry")

    def list_paths(self) -> list[Path]:
        """
        Return every canonical workspace root this registry knows about.
        """
        with self.mutex:
            for attempt in range(2):
                try:
                    return locked_registry_operation(
                        lambda: list_registered_workspace_paths_from_store(self.path),
                        path=self.path,
                    )
                except TimeoutError as exc:
                    raise WorkspaceRegistryError(str(exc)) from exc
                except sqlite3.DatabaseError as exc:
                    if attempt == 0 and self.quarantine_corrupt(str(exc)) is not None:
                        continue
                    raise WorkspaceRegistryError(f"failed to read workspace registry {self.path}: {exc}") from exc
                except OSError as exc:
                    raise WorkspaceRegistryError(f"failed to read workspace registry {self.path}: {exc}") from exc
        raise WorkspaceRegistryError(f"failed to read workspace registry {self.path}")

    def register_path(self, root: Path) -> None:
        """
        Upsert a workspace root with the current timestamp.

        The shape is intentionally a little heavier than a direct SQL
        call: root canonicalization deduplicates symlinks/relative
        paths, the mutex + retry wrapper handles concurrent CLI
        processes, and the one-shot quarantine retry lets a corrupt
        global registry rebuild itself without blocking workspace
        bootstrap forever.
        """
        resolved = canonical_workspace_root(root)
        if resolved is None:
            return
        with self.mutex:
            for attempt in range(2):
                try:
                    locked_registry_operation(
                        lambda: register_workspace_path_in_store(self.path, resolved),
                        path=self.path,
                    )
                    return
                except TimeoutError as exc:
                    raise WorkspaceRegistryError(str(exc)) from exc
                except sqlite3.DatabaseError as exc:
                    if attempt == 0 and self.quarantine_corrupt(str(exc)) is not None:
                        continue
                    raise WorkspaceRegistryError(f"failed to update workspace registry {self.path}: {exc}") from exc
                except OSError as exc:
                    raise WorkspaceRegistryError(f"failed to update workspace registry {self.path}: {exc}") from exc


def workspace_registry_path() -> Path:
    """
    Resolve the cross-workspace SQLite registry file path.

    Lives under the user's litehive root so a single shared file
    spans every workspace they have ever initialized; without that
    shared file, multi-workspace discovery would have to scan the
    filesystem.
    """
    return litehive_root() / "workspaces.db"


def build_workspace_registry(
    path: Path | None = None,
    mutex: AbstractContextManager[object] | None = None,
) -> WorkspaceRegistry:
    """
    Assemble a workspace-registry dependency.

    The default uses the user's global Litehive registry path and the
    process-local mutex. Tests and future containers can pass either
    dependency explicitly to avoid hard-coded globals.
    """
    if path is None:
        registry_path = workspace_registry_path()
    else:
        registry_path = path
    if mutex is None:
        registry_mutex = _REGISTRY_MUTEX
    else:
        registry_mutex = mutex
    return WorkspaceRegistry(path=registry_path, mutex=registry_mutex)


def default_workspace_registry() -> WorkspaceRegistry:
    """
    Return the production registry dependency for public wrappers.
    """
    return build_workspace_registry()


def workspace_registry_error() -> str | None:
    """
    Probe the registry for corruption and return a short error label.

    Returns ``None`` when the registry is healthy or absent.
    Consumed by status diagnostics to surface "registry broken"
    without taking the registry write lock; the cheap
    ``PRAGMA quick_check`` is enough to flag a corrupt file
    before a more expensive operation hits the same problem.
    """
    return default_workspace_registry().error()


def quarantine_corrupt_workspace_registry(reason: str) -> Path | None:
    """
    Move a corrupt registry file aside under a timestamped name.

    Called inline by the read/write helpers when SQLite raises
    ``DatabaseError`` so the next operation can recreate a fresh
    registry instead of failing forever. The quarantined file is
    preserved (with the failure reason in a log line) so an
    operator can inspect it after the fact.
    """
    return default_workspace_registry().quarantine_corrupt(reason)


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
    return default_workspace_registry().list_paths()


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
    default_workspace_registry().register_path(root)

