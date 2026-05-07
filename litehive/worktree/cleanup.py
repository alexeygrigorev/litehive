"""
Listing and cleanup of Litehive-managed task worktrees.

Three jobs: enumerate live managed worktrees with their dirty count
(``collect_managed_worktrees``), remove a single terminal task's
worktree on the lifecycle hand-off (``cleanup_terminal_task_worktree``),
and bulk-remove every cleanable worktree from the operator CLI
(``remove_cleanable_worktrees``). Lives as a sibling of
``litehive.worktree`` so callers that only need cleanup don't have
to import the full ``WorktreeService`` graph.
"""

from pathlib import Path
from typing import TypedDict

from litehive.attention import append_attention_log
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.domain.worktree import ManagedWorktree
from litehive.git.ops import GitError, delete_branch, remove_worktree, status_porcelain
from litehive.state.persist import load_state_for_workspace
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
from litehive.workspace import Workspace


def cleanup_terminal_task_worktree(root: Path, task: TaskRecord) -> None:
    """
    Path-based compatibility wrapper for terminal worktree cleanup.
    """
    cleanup_terminal_task_worktree_for_workspace(Workspace.from_path(root), task)


def cleanup_terminal_task_worktree_for_workspace(workspace: Workspace, task: TaskRecord) -> None:
    """
    Remove a terminal task's worktree, drop its branch, and clear task metadata.

    The lifecycle orchestrator calls this on terminal transitions
    (done/closed/cancelled) so finished tasks don't leave abandoned
    worktrees and branches lying around. The branch deletion is
    best-effort because git refuses to drop a branch a worktree
    still holds — we delete the worktree first and then sweep up.
    """
    root = workspace.root
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
    """
    Path-based compatibility wrapper for managed worktree collection.
    """
    return collect_managed_worktrees_for_workspace(Workspace.from_path(root))


def collect_managed_worktrees_for_workspace(workspace: Workspace) -> list[ManagedWorktree]:
    """
    Enumerate live Litehive-managed task worktrees with their dirty-change count.

    Backs the ``litehive worktree`` listing CLI and any flow that
    needs to render "what's on disk right now" — including the
    cleanup decision in ``remove_cleanable_worktrees``. Sorts by
    task id so two operator invocations produce identical output
    and tests don't have to depend on filesystem walk order.
    """
    root = workspace.root
    state = load_state_for_workspace(workspace)
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


class WorktreeCleanupResult(TypedDict):
    candidates: list[ManagedWorktree]
    skipped_active: list[ManagedWorktree]
    removed: list[ManagedWorktree]
    deferred: list[ManagedWorktree]
    failures: list[tuple[ManagedWorktree, str]]


def remove_cleanable_worktrees_for_workspace(workspace: Workspace, dry_run: bool = False) -> WorktreeCleanupResult:
    """
    Remove worktrees for terminal tasks using an injected workspace.

    Backs ``litehive worktree clean``. The result dict separates
    candidates by what actually happened (removed, deferred because
    the workspace was locked, failed with a git error, skipped
    because the task is still active) so the CLI can render an
    accurate per-row outcome instead of a single boolean. ``dry_run``
    returns the candidate list without touching disk for the
    ``--dry-run`` flag.
    """
    root = workspace.root
    worktrees = collect_managed_worktrees_for_workspace(workspace)
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
                        workspace,
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
