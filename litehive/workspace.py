"""Workspace value object: the bundle of identity, storage, and config every workspace operation needs.

Workspaces today are passed around as ``root: Path`` and every helper that
wants the SQLite store, a config layer, or a runtime subpath re-imports a
small set of utilities (``connect_workspace_db``, ``load_config``,
``workspace_path``, ``task_dir``) and threads ``root`` through all internal
helpers. The forwarding adds noise without adding meaning, and obscures the
real shape of dependencies — many helpers only need *one* of those handles
but accept ``root`` because that is the lowest common denominator.

``Workspace`` collapses the three handles into a single value object so a
function can declare "I need the DB" or "I need the config" by calling the
right method, and so callers stop forwarding ``root`` through chains of
private helpers (R12 step B). The constructor is the only place that does
the boundary check — everything downstream takes a ready-made
``Workspace``.

Lives at the package top level (rather than under ``domain/``) because it
imports IO handles (``connect_workspace_db``, ``load_config``); the
``domain/`` package is reserved for pure data records by the architecture
guardrail (see ``tests/test_architecture_guardrails.py``).

This is an *incremental* migration: only the feature areas explicitly
ported take ``Workspace``; the rest still take ``root: Path``. Each new
area can be moved one at a time as ergonomics warrant.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from litehive.config.paths import workspace_path
from litehive.config.workspace import normalize_workspace_root
from litehive.config.workspace_files import workspace_dir
from litehive.db.schema import connect_workspace_db

if TYPE_CHECKING:
    from litehive.config.model import LitehiveConfig
    from litehive.domain.task import TaskRecord


@dataclass(frozen=True)
class Workspace:
    """Bundle of workspace identity, on-demand SQLite access, lazy config, and subpath helpers.

    Frozen so it can be safely shared across helpers, passed into closures, and used
    as a dataclass field of other records without surprising aliasing. The cached
    config lives in a one-slot mutable holder (``_config_cache``) so freezing the
    outer object doesn't disable the cache.

    Construct via :meth:`from_path` at the boundary (CLI entry, daemon startup,
    test fixtures) — that constructor runs the workspace existence check that
    used to live inline in dozens of callers. Internal helpers should accept a
    ready-made ``Workspace`` rather than running the check again.
    """

    root: Path
    """Resolved absolute workspace root path. Identity of the workspace; used as the
    cache key for the SQLite connection and the directory anchor for
    workspace-scoped artifacts. Always normalized through ``normalize_workspace_root``
    by :meth:`from_path` so equality and path arithmetic stay consistent.
    """

    _config_cache: list = field(default_factory=list, repr=False, compare=False)
    """Single-slot holder for the lazily loaded merged config. A ``list`` (rather than
    a plain attribute) so the dataclass can stay frozen while the cache fills on
    first :meth:`config` access. Excluded from repr/equality because it is
    derived state, not identity.
    """

    @classmethod
    def from_path(cls, root: Path) -> "Workspace":
        """Boundary constructor: validate the root and return a ``Workspace``.

        This is the only place a ``Path`` becomes a ``Workspace``. Runs
        ``normalize_workspace_root`` so unresolved shell variables, paths nested
        inside ``.litehive/`` control directories, and managed-worktree paths
        are rejected up front — every downstream helper can then trust the
        ``root`` field without re-validating.

        CLI entry points and daemon startup should call this once and pass the
        resulting ``Workspace`` through the call graph. Tests that need a
        ``Workspace`` for a ``tmp_path`` workspace should also go through
        ``from_path`` so they exercise the same validation as production.
        """
        normalized = normalize_workspace_root(root, source="Workspace.from_path")
        return cls(root=normalized)

    @contextmanager
    def connect(self, migrate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield a SQLite connection for this workspace's runtime database.

        Thin instance-method wrapper over ``connect_workspace_db(self.root)`` so
        callers that hold a ``Workspace`` don't need to import the schema module
        or remember the root-keyed pragma/migration plumbing. Behaviour is
        identical to the free function — a context manager that commits on
        success, rolls back on exception, and closes on exit.
        """
        with connect_workspace_db(self.root, migrate=migrate) as connection:
            yield connection

    def config(self) -> "LitehiveConfig":
        """Return the merged ``LitehiveConfig`` for this workspace, cached after the first call.

        The merge fans defaults + user-global + per-workspace YAML + audited
        runtime settings, runs validation, and ensures the workspace exists on
        disk. That work is non-trivial and idempotent, so we cache the result
        on the instance: a single ``Workspace`` should not reload its own
        config repeatedly within one CLI invocation. Different ``Workspace``
        instances pointing at the same root each maintain their own cache —
        that is intentional, because tests sometimes want to observe a fresh
        load.
        """
        if not self._config_cache:
            # inline: config.loading transitively re-imports parts of
            # config/, so we keep the import local to avoid cycles when
            # this module is imported during partial init.
            from litehive.config.loading import load_config  # noqa: PLC0415

            self._config_cache.append(load_config(self.root))
        return self._config_cache[0]

    def runtime_dir(self) -> Path:
        """Return the workspace's hashed runtime directory under ``litehive_root``.

        Convenience accessor for ``workspace_path(root)`` with no extra parts —
        useful when callers want to enumerate runtime artifacts (logs, locks,
        the SQLite db file) without composing the path themselves.
        """
        return workspace_path(self.root)

    def runtime_path(self, *parts: str) -> Path:
        """Compose a path inside the workspace's runtime directory.

        Method form of ``workspace_path(root, *parts)``. Goes through the same
        helper so the global-runtime layout stays in one place; this exists so
        a holder of ``Workspace`` doesn't need to also import
        ``litehive.config.paths``.
        """
        return workspace_path(self.root, *parts)

    def control_dir(self) -> Path:
        """Return the in-repo ``.litehive/`` control directory for this workspace.

        Convenience accessor for ``workspace_dir(root)``. Distinct from
        :meth:`runtime_dir` — the control directory holds the user-visible
        config/context/gitignore inside the repo, while the runtime directory
        holds machine-managed state under ``$XDG_DATA_HOME``.
        """
        return workspace_dir(self.root)

    def task_dir(self, task: "TaskRecord", bootstrap: bool = True) -> Path:
        """Return the per-task working directory for a ``TaskRecord``.

        Forwards to ``litehive.tasks.paths.task_dir`` so the id-slug naming
        convention stays in one place. Lives on ``Workspace`` so helpers that
        already hold a workspace handle don't have to reach for a separate
        path module just to find a task's artifact directory.
        """
        # inline: tasks.paths transitively imports config.workspace which
        # would re-enter this module's import chain at module load time.
        from litehive.tasks.paths import task_dir as _task_dir  # noqa: PLC0415

        return _task_dir(self.root, task, bootstrap=bootstrap)
