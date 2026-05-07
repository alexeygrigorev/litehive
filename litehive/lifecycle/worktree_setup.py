"""Resolve worktree paths and build worktree-bound pipeline nodes.

The runner builds ``GitCommitNode``, ``GitWorktreeSyncNode``, and the
worktree-probe/repair callables for ``ReadyNode``/``PreExecRecoveryNode``
from this module. It also covers the post-run cleanup paths
(crash-interrupt marking, terminal worktree teardown, terminal commit
SHA reconciliation).
"""

from pathlib import Path

from litehive.domain.common import PipelineState, TaskExecutionStatus, TaskStatus
from litehive.domain.task import TaskRecord
from litehive.git.ops import current_head
from litehive.state.persist import load_state_for_workspace, save_state_for_workspace
from litehive.state.records import (
    get_task_worktree_path,
    set_task_commit_sha,
)
from litehive.worktree.paths import resolve_recorded_worktree_path
from litehive.worktree.service import WorktreeService
from litehive.workspace import Workspace

from .nodes.system import CommitNode, GitCommitNode, GitWorktreeSyncNode
from .persistence import SqlitePersistence, TaskNotFound, TaskState
from .runtime_sync import _MANUAL_REVIEW_FLAG_REASONS


def _resolve_worktree_for_workspace(workspace: Workspace, state: TaskState) -> Path:
    """Look up the task worktree path for a task, falling back to the workspace root when no worktree was recorded."""
    _, worktree_path = _task_recorded_worktree_for_workspace(workspace, state.task_id)
    return worktree_path or workspace.root


def _resolve_hook_execution_root_for_workspace(workspace: Workspace, state: TaskState) -> Path:
    """
    Pick the cwd for runner hooks based on stage.

    Pre-commit hooks run in the task worktree so they see the agent's
    edits; the ``after_commit`` hook runs on main because by that
    point the changes have already landed there and we want to verify
    the integrated state.
    """
    if state.stage == PipelineState.AFTER_COMMIT:
        return workspace.root
    return _resolve_worktree_for_workspace(workspace, state)


def _task_recorded_worktree_for_workspace(workspace: Workspace, task_id: str) -> tuple[TaskRecord | None, Path | None]:
    """
    Look up the task and its on-disk worktree path together.

    Commit and sync nodes need both the TaskRecord (for commit message
    rendering) and the worktree path (for the actual git work);
    returning them in one call means a single db hit per resolution
    instead of two.
    """
    root = workspace.root
    task = workspace.get_task(task_id)
    if task is None:
        return None, None
    recorded = get_task_worktree_path(task)
    if not recorded:
        return task, None
    return task, resolve_recorded_worktree_path(root, recorded)


def build_commit_node(root: Path) -> CommitNode:
    """Return the production ``GitCommitNode`` bound to this workspace; called by ``orchestration.run_task`` once per launch to wire the commit stage."""
    return build_commit_node_for_workspace(Workspace.from_path(root))


def build_commit_node_for_workspace(workspace: Workspace) -> CommitNode:
    """Return the production ``GitCommitNode`` bound to an injected workspace."""
    return GitCommitNode(
        workspace.root,
        worktree_resolver=lambda state: _resolve_worktree_for_workspace(workspace, state),
        task_resolver=lambda state: _task_recorded_worktree_for_workspace(workspace, state.task_id)[0],
    )


def _build_worktree_sync_node(workspace: Workspace) -> GitWorktreeSyncNode:
    """Return the production ``GitWorktreeSyncNode`` bound to this workspace; mirrors ``build_commit_node`` for the worktree-sync stage."""
    return GitWorktreeSyncNode(
        workspace=workspace,
        worktree_resolver=lambda state: _resolve_worktree_for_workspace(workspace, state),
    )


def _worktree_missing_probe(workspace: Workspace):
    """
    Build the worktree-existence probe for ``ReadyNode``.

    Returns a closure that asks ``WorktreeService`` whether the task
    has a recorded worktree that is no longer on disk; the runner
    uses it to decide "should this task re-run worktree setup before
    launch?" without every call site reaching into the service
    directly.
    """
    service = WorktreeService(workspace)

    def _probe(state) -> bool:
        return service.task_has_missing_recorded_worktree(state.task_id)

    return _probe


def _worktree_metadata_repair(workspace: Workspace):
    """
    Build the stale-worktree-metadata repair for ``PreExecRecoveryNode``.

    Twin of ``_worktree_missing_probe``: when the probe says a
    recorded worktree is gone, the machine calls this to wipe the
    stale path on the task record so the next launch creates a fresh
    worktree instead of failing the existence check.
    """
    service = WorktreeService(workspace)

    def _repair(state) -> None:
        service.clear_missing_recorded_worktree(state.task_id)

    return _repair


def _mark_task_interrupted_on_crash(workspace: Workspace, task: TaskRecord) -> None:
    """Best-effort cleanup when run_task raises an unexpected exception.

    Clears active_task_id and marks the task as interrupted so the next
    runner start can resume it instead of finding stale "running" state.
    """
    try:
        state = load_state_for_workspace(workspace)
        if state.active_task_id == task.id:
            state.active_task_id = None
            if task.id not in state.queue:
                state.queue.insert(0, task.id)
            save_state_for_workspace(workspace, state)
        fresh = workspace.get_task(task.id)
        if fresh is not None and fresh.runtime.pipeline.execution_status == TaskExecutionStatus.RUNNING:
            fresh.runtime.pipeline.execution_status = TaskExecutionStatus.INTERRUPTED
            fresh.status = TaskStatus.QUEUED
            workspace.save_task(fresh)
    except Exception:
        pass  # best-effort — don't mask the original crash


def _cleanup_terminal_worktree(workspace: Workspace, task: TaskRecord | None) -> None:
    """Tear down a finished task's worktree, but preserve worktrees for tasks flagged for manual review so an operator can inspect them."""
    if task is None:
        return
    fresh = workspace.get_task(task.id)
    if fresh is not None:
        task = fresh
    if task.status == TaskStatus.FLAGGED and task.flag_reason in _MANUAL_REVIEW_FLAG_REASONS:
        return
    WorktreeService(workspace).cleanup_terminal_task_worktree(task)


def reconcile_terminal_commit_sha(
    root: Path,
    task: TaskRecord | None,
    final_state: TaskState,
    persistence: SqlitePersistence,
) -> TaskRecord | None:
    """
    Path-based compatibility wrapper for terminal commit SHA reconciliation.
    """
    return reconcile_terminal_commit_sha_for_workspace(
        Workspace.from_path(root),
        task,
        final_state,
        persistence,
    )


def reconcile_terminal_commit_sha_for_workspace(
    workspace: Workspace,
    task: TaskRecord | None,
    final_state: TaskState,
    persistence: SqlitePersistence,
) -> TaskRecord | None:
    """Backfill the integration commit SHA on a DONE task when the runner path didn't already record one.

    Falls back to reloading the commit_result from sqlite, then to ``git
    rev-parse HEAD``, so terminal status views never display a DONE task with
    an empty commit_sha.
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

    root = workspace.root
    head_sha = commit_result.head_sha or current_head(root)
    if not head_sha:
        return task

    set_task_commit_sha(task, head_sha)
    workspace.save_task(task)
    return task
