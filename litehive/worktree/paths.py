"""Pure path/identity helpers for litehive-managed task worktrees.

These functions answer "where is the worktree for task X?" and "is this
path inside the litehive-managed worktree directory?". They are pure
filesystem path arithmetic — no git, no state, no I/O beyond ``resolve()``
and a single venv-symlink helper.

The richer worktree behavior (sync, rescue, cleanup) lives in
``litehive.worktree``; the dataclasses describing managed worktrees live
in ``litehive.domain.worktree``.
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
    """Get the expected worktree path for a task."""
    return workspace_path(root, "worktrees") / f"{task.id}-{task.slug}"


def task_worktree_branch(task: TaskRecord) -> str:
    """Get the branch name for a task worktree."""
    return f"litehive/{task.id}-{task.slug}"


def is_managed_worktree_path(root: Path, worktree_path: str | None) -> bool:
    """Check if a worktree path is managed by Litehive."""
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
    """Resolve a recorded worktree path to an absolute path."""
    if not worktree_path:
        return None
    path = Path(worktree_path).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def serialize_worktree_path(path: Path) -> str:
    """Serialize a worktree path for storage."""
    return str(path.expanduser().resolve())


def ensure_worktree_venv_link(root: Path, worktree_path: Path) -> Path | None:
    """Ensure the worktree has a venv symlink to the main venv.

    Used after creating a new task worktree so the agent inside it can
    invoke the same ``python``/``uv``/test runners that work at the
    repo root. Returns the symlink path on success, ``None`` when the
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
