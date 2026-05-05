"""Discovery and cleanup of Litehive-managed task worktrees.

Listing live worktrees, removing them on terminal task transitions, and bulk
cleaning closed/done tasks. Sibling of ``litehive.worktree`` so callers that
only need cleanup do not pull in the full WorktreeService graph.
"""

from pathlib import Path

from litehive.attention import append_attention_log
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.domain.worktree import ManagedWorktree
from litehive.git.ops import GitError, delete_branch, remove_worktree, status_porcelain
from litehive.state.persist import load_state
from litehive.state.records import (
    clear_task_worktree_path,
    get_task,
    get_task_worktree_path,
    list_tasks,
    save_task,
)
from litehive.worktree.paths import (
    is_managed_worktree_path,
    resolve_recorded_worktree_path,
    task_worktree_branch,
)


def cleanup_terminal_task_worktree(root: Path, task: TaskRecord) -> None:
    """Remove a terminal task's worktree and branch, then clear task metadata.

    Called by ``WorktreeService.cleanup_terminal_task_worktree`` and the
    lifecycle orchestrator on terminal transitions (done/closed/cancelled).
    """
    worktree_rel = get_task_worktree_path(task)
    if not worktree_rel:
        return
    worktree_path = resolve_recorded_worktree_path(root, worktree_rel)
    if worktree_path is not None and worktree_path.exists():
        remove_worktree(root, worktree_path, force=True)
    clear_task_worktree_path(task)
    save_task(root, task)
    branch = task_worktree_branch(task)
    delete_branch(root, branch)


def collect_managed_worktrees(root: Path) -> list[ManagedWorktree]:
    """Return live Litehive-managed task worktrees with their dirty-change count.

    Used by ``WorktreeService.collect_managed_worktrees`` and the
    ``litehive worktree`` CLI for operator listing.
    """
    state = load_state(root)
    if state.active_task_id:
        active_task = get_task(root, state.active_task_id)
    else:
        active_task = None
    if active_task is not None:
        active_path = get_task_worktree_path(active_task)
    else:
        active_path = None

    worktrees: list[ManagedWorktree] = []
    for task in list_tasks(root, strict=False):
        worktree_rel = get_task_worktree_path(task)
        if not is_managed_worktree_path(root, worktree_rel):
            continue
        worktree_path = resolve_recorded_worktree_path(root, worktree_rel)
        if worktree_path is None or not worktree_path.exists() or worktree_rel is None:
            continue
        try:
            change_count = len(status_porcelain(worktree_path))
        except GitError:
            change_count = 0
        worktrees.append(
            ManagedWorktree(
                task_id=task.id,
                status=task.status,
                worktree_rel=worktree_rel,
                worktree_path=worktree_path,
                change_count=change_count,
                active=task.id == state.active_task_id or worktree_rel == active_path,
            )
        )
    return sorted(worktrees, key=lambda item: item.task_id)


def remove_cleanable_worktrees(root: Path, dry_run: bool = False) -> dict[str, list[ManagedWorktree]]:
    """Remove worktrees for terminal tasks and report what was touched.

    Bulk operation backing ``WorktreeService.remove_cleanable_worktrees`` and
    the ``litehive worktree clean`` CLI. ``dry_run`` returns the candidate
    list without touching disk.
    """
    worktrees = collect_managed_worktrees(root)
    candidates = [item for item in worktrees if item.cleanable]
    skipped_active = [item for item in worktrees if item.active]

    if dry_run:
        return {
            "candidates": candidates,
            "skipped_active": skipped_active,
            "removed": [],
            "deferred": [],
            "failures": [],
        }

    failures = []
    removed = []
    deferred = []

    for item in candidates:
        try:
            remove_worktree(root, item.worktree_path, force=True)
            task = get_task(root, item.task_id)
            if task is not None:
                clear_task_worktree_path(task)
                try:
                    save_task(root, task)
                except WorkspaceConflictError:
                    append_attention_log(
                        root,
                        (f"deferred worktree metadata clearing for {item.task_id}: workspace locked by active runner"),
                    )
                    deferred.append(item)
                    continue
            removed.append(item)
        except GitError as exc:
            failures.append((item, str(exc)))

    return {
        "candidates": candidates,
        "skipped_active": skipped_active,
        "removed": removed,
        "deferred": deferred,
        "failures": failures,
    }
