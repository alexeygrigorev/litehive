"""Inspect and clean Litehive-managed task worktrees."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from litehive.config import ensure_workspace
from litehive.git import GitError, remove_worktree, status_porcelain
from litehive.tasks.crud import (
    clear_task_worktree_path,
    get_task,
    get_task_worktree_path,
    list_tasks,
    save_task,
)
from litehive.tasks.persistence import load_state

_CLEANABLE_STATUSES = {"done", "deferred", "wont_do", "duplicate"}


@dataclass(slots=True)
class _ManagedWorktree:
    task_id: str
    status: str
    worktree_rel: str
    worktree_path: Path
    change_count: int
    active: bool

    @property
    def cleanable(self) -> bool:
        return self.status in _CLEANABLE_STATUSES and not self.active


def _cmd_worktree_ls(args):
    ensure_workspace(args.workspace)
    worktrees = _collect_managed_worktrees(args.workspace)
    print(f"workspace: {args.workspace}")
    print(f"worktree_count: {len(worktrees)}")
    if not worktrees:
        print("worktrees: none")
        return 0
    for item in worktrees:
        print()
        print(f"task_id: {item.task_id}")
        print(f"status: {item.status}")
        print(f"change_count: {item.change_count}")
        print(f"worktree_path: {item.worktree_rel}")
        print(f"active: {'yes' if item.active else 'no'}")
    return 0


def _cmd_worktree_clean(args):
    ensure_workspace(args.workspace)
    worktrees = _collect_managed_worktrees(args.workspace)
    candidates = [item for item in worktrees if item.cleanable]
    skipped_active = [item for item in worktrees if item.active]

    print(f"workspace: {args.workspace}")
    print(f"dry_run: {'yes' if args.dry_run else 'no'}")
    for item in candidates:
        print(f"would_remove: {item.task_id} {item.status} {item.worktree_rel}")
    for item in skipped_active:
        print(f"skipped_active: {item.task_id} {item.status} {item.worktree_rel}")

    if args.dry_run:
        print(f"removed_count: 0")
        print(f"would_remove_count: {len(candidates)}")
        return 0

    failures: list[tuple[_ManagedWorktree, str]] = []
    removed: list[_ManagedWorktree] = []
    removed_count = 0
    for item in candidates:
        try:
            remove_worktree(args.workspace, item.worktree_path, force=True)
            task = get_task(args.workspace, item.task_id)
            if task is not None:
                clear_task_worktree_path(task)
                save_task(args.workspace, task)
            removed.append(item)
            removed_count += 1
        except GitError as exc:
            failures.append((item, str(exc)))

    for item in removed:
        print(f"removed: {item.task_id} {item.status} {item.worktree_rel}")
    for item, message in failures:
        print(f"remove_failed: {item.task_id} {message}")
    print(f"removed_count: {removed_count}")
    return 1 if failures else 0


def _collect_managed_worktrees(root: Path) -> list[_ManagedWorktree]:
    state = load_state(root)
    active_task = get_task(root, state.active_task_id) if state.active_task_id else None
    active_path = get_task_worktree_path(active_task) if active_task is not None else None

    worktrees: list[_ManagedWorktree] = []
    for task in list_tasks(root):
        worktree_rel = get_task_worktree_path(task)
        if not _is_litehive_managed_worktree(worktree_rel):
            continue
        worktree_path = (root / worktree_rel).resolve()
        if not worktree_path.exists():
            continue
        try:
            change_count = len(status_porcelain(worktree_path))
        except GitError:
            change_count = 0
        worktrees.append(
            _ManagedWorktree(
                task_id=task.id,
                status=task.status,
                worktree_rel=worktree_rel,
                worktree_path=worktree_path,
                change_count=change_count,
                active=task.id == state.active_task_id or worktree_rel == active_path,
            )
        )
    return sorted(worktrees, key=lambda item: item.task_id)


def _is_litehive_managed_worktree(worktree_rel: str | None) -> bool:
    if not worktree_rel:
        return False
    path = PurePosixPath(worktree_rel)
    return not path.is_absolute() and path.parts[:2] == (".litehive", "worktrees")
