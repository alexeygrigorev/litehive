"""
``Workspace`` value object bundling identity, storage, and config.

Pre-``Workspace``, helpers were threaded ``root: Path`` through
chains of private functions and re-imported the same set of utilities
(``connect_workspace_db``, ``load_config``, ``workspace_path``,
``task_dir``) at every level. The forwarding added noise without
meaning and obscured the real shape of dependencies — many helpers
only needed *one* handle but accepted ``root`` as the lowest common
denominator. ``Workspace`` collapses the three handles into a single
value object so a function declares "I need the DB" or "I need the
config" by calling the right method.

The constructor (:meth:`Workspace.from_path`) is the only place that
runs the boundary validation; everything downstream takes a ready-
made ``Workspace`` and trusts the root field. Lives at the package
top level (rather than under ``domain/``) because it imports IO
handles, and ``domain/`` is reserved for pure data records by the
architecture guardrail in ``tests/test_architecture_guardrails.py``.

This is an *incremental* migration: only ported feature areas take
``Workspace``; the rest still take ``root: Path``. Each new area
moves over as ergonomics warrant.
"""

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
    from litehive.tasks.activity import TaskActivityLog


@dataclass(frozen=True)
class Workspace:
    """
    Bundle of workspace identity, on-demand SQLite access, lazy config, and subpath helpers.

    Frozen so it can be safely shared across helpers, passed into
    closures, and used as a dataclass field of other records without
    surprising aliasing. The cached config lives in a one-slot
    mutable holder (``_config_cache``) so freezing the outer object
    doesn't disable the cache. Construct via :meth:`from_path` at
    the system boundary (CLI entry, daemon startup, test fixtures)
    — that's the only place that runs the workspace-existence check
    that used to live inline in dozens of callers. Internal helpers
    should accept a ready-made ``Workspace`` rather than running
    the check again.
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
        """
        Boundary constructor: validate ``root`` and return a ``Workspace``.

        The only place a raw ``Path`` becomes a ``Workspace``. Runs
        ``normalize_workspace_root`` so unresolved shell variables,
        paths nested inside ``.litehive/`` control directories, and
        managed-worktree paths are rejected up front — every
        downstream helper can then trust the ``root`` field without
        re-validating. Tests that need a ``Workspace`` for a
        ``tmp_path`` should also go through here so they exercise
        the same validation as production code paths.
        """
        normalized = normalize_workspace_root(root, source="Workspace.from_path")
        return cls(root=normalized)

    @contextmanager
    def connect(self, migrate: bool = True) -> Iterator[sqlite3.Connection]:
        """
        Yield a SQLite connection for this workspace's runtime database.

        Thin instance-method wrapper over ``connect_workspace_db``
        so callers holding a ``Workspace`` don't need to import the
        schema module or remember the root-keyed pragma/migration
        plumbing. Behaviour is identical to the free function:
        commits on success, rolls back on exception, closes on
        exit.
        """
        with connect_workspace_db(self.root, migrate=migrate) as connection:
            yield connection

    def config(self) -> "LitehiveConfig":
        """
        Return the merged ``LitehiveConfig`` for this workspace, cached after first call.

        The merge fans defaults + user-global + per-workspace YAML
        + audited runtime settings, runs validation, and ensures
        the workspace exists on disk. That work is non-trivial and
        idempotent, so we cache the result on the instance: a
        single ``Workspace`` should not reload its own config
        repeatedly within one CLI invocation. Different
        ``Workspace`` instances at the same root each keep their
        own cache so tests can observe a fresh load when they want.
        """
        if not self._config_cache:
            # inline: config.loading transitively re-imports parts of
            # config/, so we keep the import local to avoid cycles when
            # this module is imported during partial init.
            from litehive.config.loading import load_config_for_workspace  # noqa: PLC0415

            self._config_cache.append(load_config_for_workspace(self))
        return self._config_cache[0]

    def runtime_dir(self) -> Path:
        """
        Return the workspace's hashed runtime directory under ``litehive_root``.

        Convenience accessor for ``workspace_path(root)`` with no
        extra parts — useful when callers want to enumerate runtime
        artifacts (logs, locks, ``data.db``) without composing the
        path themselves and risking off-by-one path joining.
        """
        return workspace_path(self.root)

    def runtime_path(self, *parts: str) -> Path:
        """
        Compose a path inside the workspace's runtime directory.

        Method form of ``workspace_path(root, *parts)``. Goes
        through the same helper so the global-runtime layout
        (hashed-by-root, under ``$XDG_DATA_HOME``) stays in one
        place; exists so a ``Workspace`` holder doesn't need to
        also import ``litehive.config.paths``.
        """
        return workspace_path(self.root, *parts)

    def control_dir(self) -> Path:
        """
        Return the in-repo ``.litehive/`` control directory.

        Distinct from :meth:`runtime_dir` — the control directory
        holds the user-visible config/context/gitignore that's
        committed to the repo, while the runtime directory holds
        machine-managed state outside the repo. Operators edit the
        former; the daemon owns the latter.
        """
        return workspace_dir(self.root)

    def task_dir(self, task: "TaskRecord", bootstrap: bool = True) -> Path:
        """
        Return the per-task working directory for a ``TaskRecord``.

        Forwards to ``litehive.tasks.paths.task_dir`` so the
        id-slug naming convention stays in one place. Lives on
        ``Workspace`` so helpers already holding a workspace
        handle don't have to reach for a separate path module
        just to find a task's artifact directory.
        """
        # inline: tasks.paths transitively imports config.workspace which
        # would re-enter this module's import chain at module load time.
        from litehive.tasks.paths import task_dir as _task_dir  # noqa: PLC0415

        return _task_dir(self.root, task, bootstrap=bootstrap)

    def list_tasks(self, include_runtime: bool = True, strict: bool = True) -> list["TaskRecord"]:
        """
        Return task records for this workspace.

        Method form of ``litehive.state.records.list_tasks`` so
        workspace-aware helpers do not need to re-thread ``root`` for
        ordinary task lookup.
        """
        # inline: state.records imports several modules that eventually
        # refer back to Workspace during partial startup.
        from litehive.state.records import list_tasks as _list_tasks  # noqa: PLC0415

        return _list_tasks(self.root, include_runtime=include_runtime, strict=strict)

    def get_task(self, task_id: str) -> "TaskRecord | None":
        """
        Return one task record from this workspace, or ``None`` when missing.
        """
        # inline: see list_tasks import note above.
        from litehive.state.records import get_task as _get_task  # noqa: PLC0415

        return _get_task(self.root, task_id)

    def get_task_record(self, task_id: str) -> "TaskRecord | None":
        """
        Return one task record, tolerating a missing runtime row.
        """
        # inline: see list_tasks import note above.
        from litehive.state.records import get_task_record as _get_task_record  # noqa: PLC0415

        return _get_task_record(self.root, task_id)

    def require_task(self, task_id: str) -> "TaskRecord":
        """
        Return one task record or raise the standard missing-task error.
        """
        # inline: see list_tasks import note above.
        from litehive.state.records import require_task as _require_task  # noqa: PLC0415

        return _require_task(self.root, task_id)

    def save_task(self, task: "TaskRecord") -> None:
        """
        Persist a task record in this workspace.
        """
        # inline: see list_tasks import note above.
        from litehive.state.records import save_task as _save_task  # noqa: PLC0415

        _save_task(self.root, task)

    def task_activity(self, task: "TaskRecord") -> "TaskActivityLog":
        """
        Return the persisted activity feed handle for ``task``.

        Query operations live on the returned collaborator so callers
        holding a ``Workspace`` can ask for task activity directly
        without threading both objects through loose helper functions.
        """
        # inline: tasks.activity imports Workspace for type annotations,
        # so importing at module load would create an import cycle.
        from litehive.tasks.activity import TaskActivityLog  # noqa: PLC0415

        return TaskActivityLog(self, task)

    def append_event(self, task: "TaskRecord", event) -> dict:
        """
        Append a typed task event to this workspace's durable event stream.
        """
        # inline: observability.events imports Workspace for annotations,
        # so importing at module load would create an import cycle.
        from litehive.observability.events import append_event  # noqa: PLC0415

        return append_event(self, task, event)
