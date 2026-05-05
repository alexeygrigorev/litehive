"""
Pure path/identity helpers for managed task worktrees.

Answers "where is the worktree for task X?", "is this path inside
the litehive-managed area?", and produces the canonical absolute
string we persist on the task record. No git, no state, no I/O
beyond ``resolve()`` — except for :func:`ensure_worktree_venv_link`,
which is the one place that touches disk because every caller that
creates a worktree wants the venv link in the same step.

Richer worktree behaviour (sync/rescue/cleanup) lives in sibling
modules; the dataclasses describing managed worktrees live in
``litehive.domain.worktree``.
"""

import logging
from pathlib import Path

from litehive.config.paths import workspace_path
from litehive.domain.task import TaskRecord
from litehive.fs_cleanup import remove_tree_logged

# Use the ``litehive.worktree`` namespace so caplog filters that watch
# the worktree subsystem catch our messages too — these helpers were
# previously inside ``litehive.worktree`` and the existing tests still
# scope to that logger.
logger = logging.getLogger("litehive.worktree.paths")


def task_worktree_path(root: Path, task: TaskRecord) -> Path:
    """
    Compute the canonical worktree location for a task.

    Layout is ``<workspace>/worktrees/<id>-<slug>`` so creation and
    lookup agree on placement; if the two sides ever disagreed,
    ``WorktreeService.sync_task_worktree`` would create a duplicate
    worktree on each call. Centralizing the layout here is the only
    place to change it.
    """
    return workspace_path(root, "worktrees") / f"{task.id}-{task.slug}"


def task_worktree_branch(task: TaskRecord) -> str:
    """
    Return the namespaced git branch a task's worktree commits to.

    Uses the ``litehive/<id>-<slug>`` prefix so litehive-managed
    branches don't collide with operator-authored branches and so a
    branch listing makes it obvious which branches the daemon owns.
    """
    return f"litehive/{task.id}-{task.slug}"


def is_managed_worktree_path(root: Path, worktree_path: str | None) -> bool:
    """
    Whether a stored worktree path belongs to the litehive-managed tree.

    Rescue and cleanup use this to refuse touching paths the operator
    moved or hand-edited — modifying a path outside the
    ``worktrees/`` directory would surprise the operator and could
    delete unrelated work. ``None`` and relative paths are treated
    as "not managed" because we can't reason about them safely.
    """
    if not worktree_path:
        return False
    path = Path(worktree_path).expanduser()
    if not path.is_absolute():
        return False
    try:
        return path.resolve().is_relative_to(workspace_path(root, "worktrees").resolve())
    except OSError:
        return False


def resolve_recorded_worktree_path(root: Path, worktree_path: str | None) -> Path | None:
    """
    Turn a stored worktree path back into an absolute resolved ``Path``.

    Pairs with :func:`serialize_worktree_path`: serializing always
    writes an absolute string, but old records may carry a
    workspace-relative path. Treating relative entries as
    workspace-relative keeps those legacy records readable.
    Returns ``None`` when no path was recorded so callers branch
    once on "no worktree" instead of on every individual field.
    """
    if not worktree_path:
        return None
    path = Path(worktree_path).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def serialize_worktree_path(path: Path) -> str:
    """
    Render a worktree path as the canonical absolute string for the task record.

    Always resolves so two callers passing the same logical path
    produce identical stored strings — without that, the round-trip
    through :func:`resolve_recorded_worktree_path` could re-resolve
    differently and break equality tests in the cleanup flow.
    """
    return str(path.expanduser().resolve())


def ensure_worktree_venv_link(root: Path, worktree_path: Path) -> Path | None:
    """
    Symlink the worktree's ``.venv`` to the main repo's venv.

    Used after creating a new task worktree so the agent inside it
    can invoke the same ``python``/``uv``/test runners that work at
    the repo root. Without this, every agent would re-create its own
    venv on every task and tests that import workspace-installed
    packages would silently differ between the main checkout and the
    worktree. Returns the symlink path on success, ``None`` when the
    main repo has no ``.venv`` to link against.
    """
    main_venv = (root / ".venv").expanduser()
    if not (main_venv.exists() or main_venv.is_symlink()):
        return None

    worktree_venv = worktree_path / ".venv"
    if worktree_venv.is_symlink() and worktree_venv.resolve() == main_venv.resolve():
        return worktree_venv

    if worktree_venv.is_symlink() or worktree_venv.exists():
        if worktree_venv.is_dir() and not worktree_venv.is_symlink():
            remove_tree_logged(
                worktree_venv,
                logger=logger,
                target_label="worktree venv directory",
            )
        else:
            worktree_venv.unlink()

    worktree_venv.symlink_to(main_venv, target_is_directory=main_venv.is_dir())
    return worktree_venv
