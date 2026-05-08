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
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from litehive.config.paths import workspace_path
from litehive.config.workspace import normalize_workspace_root
from litehive.config.workspace_files import workspace_dir
from litehive.db.schema import connect_workspace_db

if TYPE_CHECKING:
    from litehive.agents.session_store import LoadedSubagentSession
    from litehive.config.model import LitehiveConfig
    from litehive.domain.task import TaskRecord
    from litehive.tasks.activity import TaskActivityLog


class Workspace:
    """
    Bundle of workspace identity, on-demand SQLite access, lazy config, and subpath helpers.

    Construct via :meth:`from_path` at the system boundary (CLI
    entry, daemon startup, test fixtures) — that's the only place
    that runs the workspace-existence check that used to live inline
    in dozens of callers. Internal helpers should accept a ready-made
    ``Workspace`` rather than running the check again.
    """

    __slots__ = ("_config_cache", "root")

    def __init__(self, root: Path) -> None:
        """
        Store the resolved workspace root and its lazy config cache.

        ``from_path`` owns boundary validation. The constructor only
        stores dependencies so tests can still build a workspace-like
        value directly when a normalized root is already available.
        """
        self.root = root
        self._config_cache: list = []

    def __repr__(self) -> str:
        return f"Workspace(root={self.root!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Workspace):
            return NotImplemented
        return self.root == other.root

    def __hash__(self) -> int:
        return hash(self.root)

    @classmethod
    def from_path(cls, root: Path) -> "Workspace":
        """
        Boundary constructor: validate ``root`` and return a ``Workspace``.

        The only place a raw ``Path`` becomes a ``Workspace``. Runs
        ``normalize_workspace_root`` so paths nested inside
        ``.litehive/`` control directories and managed-worktree
        paths are rejected up front — every
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

    def load_config(self) -> "LitehiveConfig":
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

    def config(self) -> "LitehiveConfig":
        """
        Compatibility wrapper for callers not yet migrated to ``load_config``.
        """
        return self.load_config()

    def require_existing(self, source: str) -> Path:
        """
        Validate that this workspace exists on disk.

        Method form of ``require_existing_workspace`` so callers already
        holding a ``Workspace`` do not peel out ``root`` to cross the same
        boundary again.
        """
        # inline: config.workspace imports this module during boundary construction.
        from litehive.config.workspace import require_existing_workspace  # noqa: PLC0415

        return require_existing_workspace(self.root, source=source)

    def create(self) -> Path:
        """
        Bootstrap this workspace's control files and runtime store.

        Method form of ``create_workspace`` for creation flows that already
        hold an injected ``Workspace``.
        """
        # inline: config.workspace imports this module during bootstrap.
        from litehive.config.workspace import create_workspace  # noqa: PLC0415

        return create_workspace(self.root)

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

        Method form of ``litehive.state.records.list_tasks_for_workspace`` so
        workspace-aware helpers do not need to re-thread ``root`` for
        ordinary task lookup.
        """
        # inline: state.records imports several modules that eventually
        # refer back to Workspace during partial startup.
        from litehive.state.records import list_tasks_for_workspace as _list_tasks_for_workspace  # noqa: PLC0415

        return _list_tasks_for_workspace(self, include_runtime=include_runtime, strict=strict)

    def get_task(self, task_id: str) -> "TaskRecord | None":
        """
        Return one task record from this workspace, or ``None`` when missing.
        """
        # inline: see list_tasks import note above.
        from litehive.state.records import get_task_for_workspace as _get_task_for_workspace  # noqa: PLC0415

        return _get_task_for_workspace(self, task_id)

    def get_task_record(self, task_id: str) -> "TaskRecord | None":
        """
        Return one task record, tolerating a missing runtime row.
        """
        # inline: see list_tasks import note above.
        from litehive.state.records import get_task_record_for_workspace as _get_task_record_for_workspace  # noqa: PLC0415

        return _get_task_record_for_workspace(self, task_id)

    def require_task(self, task_id: str) -> "TaskRecord":
        """
        Return one task record or raise the standard missing-task error.
        """
        # inline: see list_tasks import note above.
        from litehive.state.records import require_task_for_workspace as _require_task_for_workspace  # noqa: PLC0415

        return _require_task_for_workspace(self, task_id)

    def save_task(self, task: "TaskRecord") -> None:
        """
        Persist a task record in this workspace.
        """
        # inline: see list_tasks import note above.
        from litehive.state.records import save_task_for_workspace as _save_task_for_workspace  # noqa: PLC0415

        _save_task_for_workspace(self, task)

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

    def load_subagent_session(self, task_id: str, subagent_id: str) -> "LoadedSubagentSession":
        """
        Return the typed subagent session slice owned by this workspace.
        """
        # inline: session_store imports Workspace for runtime access, so
        # importing at module load would create an import cycle.
        from litehive.agents.session_store import load_subagent_session_record  # noqa: PLC0415

        return load_subagent_session_record(self, task_id, subagent_id)

    def load_subagent_session_record(self, task_id: str, subagent_id: str) -> "LoadedSubagentSession":
        """
        Return the typed subagent session slice owned by this workspace.
        """
        # inline: session_store imports Workspace for runtime access, so
        # importing at module load would create an import cycle.
        from litehive.agents.session_store import load_subagent_session_record  # noqa: PLC0415

        return load_subagent_session_record(self, task_id, subagent_id)

    def load_subagent_session_created_at(self, task_id: str, subagent_id: str) -> str | None:
        """
        Return the persisted creation timestamp for one subagent session.
        """
        return self.load_subagent_session_record(task_id, subagent_id).created_at
