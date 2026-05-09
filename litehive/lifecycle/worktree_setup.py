"""Resolve worktree paths and build worktree-bound pipeline nodes.

The runner builds ``GitCommitNode``, ``GitWorktreeSyncNode``, and the
worktree-probe/repair callables for ``ReadyNode``/``PreExecRecoveryNode``
from this module. It also covers the post-run cleanup paths
(crash-interrupt marking, terminal worktree teardown, terminal commit
SHA reconciliation).
"""

from collections.abc import Callable
from pathlib import Path

from litehive.domain.common import PipelineState, TaskExecutionStatus, TaskStatus
from litehive.domain.task import TaskRecord
from litehive.git.ops import current_head
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import (
    clear_task_worktree_path,
    get_task_worktree_path,
    set_task_commit_sha,
    WorkspaceTasks,
)
from litehive.worktree.cleanup import WorktreeCleanupService
from litehive.worktree.inspection import WorktreeInspector
from litehive.worktree.paths import WorktreePaths
from litehive.workspace import Workspace

from .nodes.system import CommitNode, GitCommitNode, GitWorktreeSyncNode
from .persistence import SqlitePersistence, TaskNotFound, TaskState
from .runtime_sync import _MANUAL_REVIEW_FLAG_REASONS


class PipelineWorktreeSetup:
    """
    Workspace-bound owner for lifecycle worktree dependency setup.

    The runner needs the same task/worktree lookup rules for hooks, commit,
    worktree sync, recovery probes, crash cleanup, and terminal reconciliation.
    Binding the workspace once keeps those rules in one object instead of
    scattering small workspace-first helpers through orchestration.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.tasks = WorkspaceTasks(workspace)
        self.paths = WorktreePaths(workspace)

    def resolve_worktree(self, state: TaskState) -> Path:
        """Look up the task worktree path for a task, falling back to the workspace root when no worktree was recorded."""
        _, worktree_path = self.task_recorded_worktree(state.task_id)
        return worktree_path or self.workspace.root

    def resolve_hook_execution_root(self, state: TaskState) -> Path:
        """
        Pick the cwd for runner hooks based on stage.

        Pre-commit hooks run in the task worktree so they see the agent's
        edits; the ``after_commit`` hook runs on main because by that point the
        changes have already landed there and we want to verify the integrated
        state.
        """
        if state.stage == PipelineState.AFTER_COMMIT:
            return self.workspace.root
        return self.resolve_worktree(state)

    def task_recorded_worktree(self, task_id: str) -> tuple[TaskRecord | None, Path | None]:
        """
        Look up the task and its on-disk worktree path together.

        Commit and sync nodes need both the TaskRecord (for commit message
        rendering) and the worktree path (for the actual git work); returning
        them in one call means a single db hit per resolution instead of two.
        """
        task = self.tasks.get(task_id)
        if task is None:
            return None, None
        recorded = get_task_worktree_path(task)
        if not recorded:
            return task, None
        return task, self.paths.resolve_recorded_worktree_path(recorded)

    def build_commit_node(self) -> CommitNode:
        """Return the production ``GitCommitNode`` bound to this workspace."""
        return GitCommitNode(
            self.workspace,
            worktree_resolver=self.resolve_worktree,
            task_resolver=lambda state: self.task_recorded_worktree(state.task_id)[0],
        )

    def build_worktree_sync_node(self) -> GitWorktreeSyncNode:
        """Return the production ``GitWorktreeSyncNode`` bound to this workspace."""
        return GitWorktreeSyncNode(
            workspace=self.workspace,
            worktree_resolver=self.resolve_worktree,
        )

    def worktree_missing_probe(self) -> Callable[[TaskState], bool]:
        """
        Build the worktree-existence probe for ``ReadyNode``.

        Returns a closure that asks ``WorktreeInspector`` whether the task has
        a recorded worktree that is no longer on disk; the runner uses it to
        decide whether worktree setup should run again before launch.
        """
        inspector = WorktreeInspector(self.workspace)

        def _probe(state: TaskState) -> bool:
            task = self.tasks.get(state.task_id)
            if task is None:
                return False
            inspection = inspector.inspect_task_worktree(task)
            return inspection.worktree_rel is not None and not inspection.exists

        return _probe

    def worktree_metadata_repair(self) -> Callable[[TaskState], None]:
        """
        Build the stale-worktree-metadata repair for ``PreExecRecoveryNode``.

        Twin of ``worktree_missing_probe``: when the probe says a recorded
        worktree is gone, the machine calls this to wipe the stale path on the
        task record so the next launch creates a fresh worktree.
        """
        inspector = WorktreeInspector(self.workspace)

        def _repair(state: TaskState) -> None:
            task = self.tasks.get(state.task_id)
            if task is None:
                return
            inspection = inspector.inspect_task_worktree(task)
            if inspection.worktree_rel is None or inspection.exists:
                return
            clear_task_worktree_path(task)
            self.tasks.save(task)

        return _repair

    def mark_task_interrupted_on_crash(self, task: TaskRecord) -> None:
        """
        Best-effort cleanup when ``run_task`` raises an unexpected exception.

        Clears active_task_id and marks the task as interrupted so the next
        runner start can resume it instead of finding stale "running" state.
        """
        try:
            state = WorkspaceStateRepository(self.workspace).load()
            if state.active_task_id == task.id:
                state.active_task_id = None
                if task.id not in state.queue:
                    state.queue.insert(0, task.id)
                WorkspaceStateRepository(self.workspace).save(state)
            fresh = self.tasks.get(task.id)
            if fresh is not None and fresh.runtime.pipeline.execution_status == TaskExecutionStatus.RUNNING:
                fresh.runtime.pipeline.execution_status = TaskExecutionStatus.INTERRUPTED
                fresh.status = TaskStatus.QUEUED
                self.tasks.save(fresh)
        except Exception:
            pass  # best-effort; do not mask the original crash

    def cleanup_terminal_worktree(self, task: TaskRecord | None) -> None:
        """Tear down a finished task's worktree, preserving manual-review worktrees for inspection."""
        if task is None:
            return
        fresh = self.tasks.get(task.id)
        if fresh is not None:
            task = fresh
        if task.status == TaskStatus.FLAGGED and task.flag_reason in _MANUAL_REVIEW_FLAG_REASONS:
            return
        WorktreeCleanupService(self.workspace).cleanup_terminal_task_worktree(task)

    def reconcile_terminal_commit_sha(
        self,
        task: TaskRecord | None,
        final_state: TaskState,
        persistence: SqlitePersistence,
    ) -> TaskRecord | None:
        """
        Backfill the integration commit SHA on a DONE task when the runner path did not record one.

        Falls back to reloading the commit_result from sqlite, then to ``git
        rev-parse HEAD``, so terminal status views never display a DONE task
        with an empty commit_sha.
        """
        if task is None or final_state.stage != PipelineState.DONE:
            return task
        if task.git.commit_sha and task.runtime.pipeline.git.commit_sha:
            return task

        commit_result = final_state.commit_result
        if commit_result is None:
            try:
                commit_result = persistence.load(final_state.task_id).commit_result
            except TaskNotFound:
                commit_result = None
        if commit_result is None:
            return task

        head_sha = commit_result.head_sha or current_head(self.workspace.root)
        if not head_sha:
            return task

        set_task_commit_sha(task, head_sha)
        self.tasks.save(task)
        return task
