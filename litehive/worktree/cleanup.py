"""
Listing and cleanup of Litehive-managed task worktrees.

Three jobs: enumerate live managed worktrees with their dirty count,
remove a single terminal task's worktree on the lifecycle hand-off,
and bulk-remove every cleanable worktree from the operator CLI. Lives
as a sibling of ``litehive.worktree`` so callers that only need cleanup
don't have to import sync or rescue code.
"""

from typing import TypedDict

from litehive.attention import AttentionRepository
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.domain.worktree import ManagedWorktree
from litehive.git.ops import GitError, delete_branch, remove_worktree, status_porcelain
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import (
    clear_task_worktree_path,
    get_task_worktree_path,
    WorkspaceTasks,
)
from litehive.worktree.paths import (
    task_worktree_branch,
    WorktreePaths,
)
from litehive.workspace import Workspace


class WorktreeCleanupResult(TypedDict):
    """
    Summary of a bulk worktree cleanup pass returned by
    ``WorktreeCleanupService.remove_cleanable_worktrees``.

    candidates lists all terminal worktrees eligible for removal.
    skipped_active lists worktrees the daemon is currently running.
    removed lists worktrees successfully deleted.
    deferred lists worktrees whose metadata could not be cleared.
    failures maps worktrees to their removal error message.
    """

    candidates: list[ManagedWorktree]
    skipped_active: list[ManagedWorktree]
    removed: list[ManagedWorktree]
    deferred: list[ManagedWorktree]
    failures: list[tuple[ManagedWorktree, str]]


class WorktreeCleanupService:
    """
    Workspace-bound listing and cleanup of Litehive-managed task worktrees.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Bind the cleanup service to one workspace and its path policy.

        The workspace reference is what lets cleanup, rescue, and
        status callers share one instance instead of reconstructing
        path helpers on every call.
        """
        self.workspace = workspace
        self.paths = WorktreePaths(workspace)

    def cleanup_terminal_task_worktree(self, task: TaskRecord) -> None:
        """
        Remove a terminal task's worktree, branch, and recorded metadata.
        """
        worktree_rel = get_task_worktree_path(task)
        if not worktree_rel:
            return
        worktree_path = self.paths.resolve_recorded_worktree_path(worktree_rel)
        if worktree_path is not None and worktree_path.exists():
            remove_worktree(self.workspace.root, worktree_path, force=True)
        clear_task_worktree_path(task)
        WorkspaceTasks(self.workspace).save(task)
        branch = task_worktree_branch(task)
        delete_branch(self.workspace.root, branch)

    def collect_managed_worktrees(self) -> list[ManagedWorktree]:
        """
        Enumerate live Litehive-managed task worktrees.
        """
        state = WorkspaceStateRepository(self.workspace).load()
        tasks = WorkspaceTasks(self.workspace)
        if state.active_task_id:
            active_task = tasks.get(state.active_task_id)
        else:
            active_task = None
        if active_task is not None:
            active_path = get_task_worktree_path(active_task)
        else:
            active_path = None

        worktrees: list[ManagedWorktree] = []
        for task in tasks.list(strict=False):
            worktree_rel = get_task_worktree_path(task)
            if not self.paths.is_managed_worktree_path(worktree_rel):
                continue
            worktree_path = self.paths.resolve_recorded_worktree_path(worktree_rel)
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

    def remove_cleanable_worktrees(self, dry_run: bool = False) -> WorktreeCleanupResult:
        """
        Remove or preview worktrees for terminal tasks.
        """
        worktrees = self.collect_managed_worktrees()
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
        attention_repository = AttentionRepository(self.workspace)

        for item in candidates:
            try:
                remove_worktree(self.workspace.root, item.worktree_path, force=True)
                task = WorkspaceTasks(self.workspace).get(item.task_id)
                if task is not None:
                    clear_task_worktree_path(task)
                    try:
                        WorkspaceTasks(self.workspace).save(task)
                    except WorkspaceConflictError:
                        attention_repository.append(
                            f"deferred worktree metadata clearing for {item.task_id}: workspace locked by active runner"
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
