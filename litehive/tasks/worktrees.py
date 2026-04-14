"""Shared helpers for Litehive-managed task worktrees."""

from pathlib import Path

from litehive.config.paths import worktree_root
from litehive.domain.task import TaskRecord


def task_worktree_path(root: Path, task: TaskRecord) -> Path:
    return worktree_root(root) / f"{task.id}-{task.slug}"


def task_worktree_branch(task: TaskRecord) -> str:
    return f"litehive/{task.id}-{task.slug}"


def is_managed_worktree_path(root: Path, worktree_path: str | None) -> bool:
    if not worktree_path:
        return False
    path = Path(worktree_path).expanduser()
    if not path.is_absolute():
        return False
    try:
        return path.resolve().is_relative_to(worktree_root(root).resolve())
    except OSError:
        return False


def resolve_recorded_worktree_path(root: Path, worktree_path: str | None) -> Path | None:
    if not worktree_path:
        return None
    path = Path(worktree_path).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def serialize_worktree_path(path: Path) -> str:
    return str(path.expanduser().resolve())
