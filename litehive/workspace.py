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

This migration is intentionally incremental: workspace-bound feature
areas should take ``Workspace`` or a focused service assembled from it,
while raw path helpers stay at process, config, database, git, sandbox,
or other explicit filesystem boundaries.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from litehive.config.paths import workspace_path
from litehive.config.workspace import normalize_workspace_root
from litehive.config.workspace_files import WorkspaceControlFiles
from litehive.db.schema import connect_workspace_db

if TYPE_CHECKING:
    from litehive.config.model import LitehiveConfig
    from litehive.domain.task import TaskRecord


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
            from litehive.config.loading import WorkspaceConfigLoader  # noqa: PLC0415

            self._config_cache.append(WorkspaceConfigLoader(self).load())
        return self._config_cache[0]

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
        return self.control_files().directory()

    def control_files(self) -> WorkspaceControlFiles:
        """
        Return the bound repo-local ``.litehive`` path object.

        Keeps config, context, and gitignore paths attached to this
        workspace's validated root so downstream code does not need to
        thread ``root`` back through scattered path helpers.
        """
        return WorkspaceControlFiles(self.root)

    def task_dir(self, task: "TaskRecord", bootstrap: bool = True) -> Path:
        """
        Return the per-task working directory for a ``TaskRecord``.

        Transparently redirects through the per-worktree ``.litehive/`` when
        this workspace points inside a managed worktree, so artifacts land next
        to the matching checkout instead of the main workspace.
        """
        tasks = self._tasks_dir(bootstrap=bootstrap)
        return tasks / f"{task.id}-{task.slug}"

    def _tasks_dir(self, bootstrap: bool = True) -> Path:
        """
        Return the directory containing task artifacts for this workspace.
        """
        worktree_workspace = self._worktree_workspace_dir()
        if worktree_workspace is not None:
            return worktree_workspace / "tasks"
        if bootstrap:
            self.create()
        tasks = self.control_dir() / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        return tasks

    def _worktree_workspace_dir(self) -> Path | None:
        """
        Resolve the canonical ``.litehive`` directory for a managed worktree.
        """
        resolved = self.root.resolve()
        parts = resolved.parts
        for i, part in enumerate(parts):
            if part == ".litehive" and i + 2 < len(parts) and parts[i + 1] == "worktrees":
                return Path(*parts[: i + 3]) / ".litehive"
        return None
