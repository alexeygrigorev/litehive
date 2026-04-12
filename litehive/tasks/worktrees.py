"""Shared helpers for Litehive-managed task worktrees."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from litehive.config import worktree_root
from litehive.git import GitError, move_worktree, prune_worktrees
from litehive.models import TaskRecord

_LEGACY_PREFIX = (".litehive", "worktrees")


def legacy_worktree_root(root: Path) -> Path:
    return root / ".litehive" / "worktrees"


def task_worktree_path(root: Path, task: TaskRecord) -> Path:
    return worktree_root(root) / f"{task.id}-{task.slug}"


def is_legacy_worktree_path(worktree_path: str | None) -> bool:
    if not worktree_path:
        return False
    path = PurePosixPath(worktree_path)
    return not path.is_absolute() and path.parts[:2] == _LEGACY_PREFIX


def is_managed_worktree_path(root: Path, worktree_path: str | None) -> bool:
    if not worktree_path:
        return False
    if is_legacy_worktree_path(worktree_path):
        return True
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


def migrate_legacy_worktree(root: Path, task: TaskRecord) -> tuple[Path | None, bool]:
    recorded = task.runtime.git.worktree_path or task.git.worktree_path
    if not recorded:
        return None, False
    resolved = resolve_recorded_worktree_path(root, recorded)
    if not is_legacy_worktree_path(recorded):
        return resolved, False
    if resolved is None or not resolved.exists():
        task.runtime.git.worktree_path = None
        task.git.worktree_path = None
        try:
            prune_worktrees(root)
        except GitError:
            pass
        return None, True

    destination = task_worktree_path(root, task)
    destination.parent.mkdir(parents=True, exist_ok=True)
    move_worktree(root, resolved, destination)
    task.runtime.git.worktree_path = serialize_worktree_path(destination)
    task.git.worktree_path = None
    return destination.resolve(), True
