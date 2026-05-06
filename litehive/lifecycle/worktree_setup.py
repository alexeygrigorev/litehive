"""Resolve worktree paths and build worktree-bound pipeline nodes.

The runner builds ``GitCommitNode``, ``GitWorktreeSyncNode``, and the
worktree-probe/repair callables for ``ReadyNode``/``PreExecRecoveryNode``
from this module. It also covers the post-run cleanup paths
(crash-interrupt marking, terminal worktree teardown, terminal commit
SHA reconciliation).
"""

from pathlib import Path

from litehive.domain.common import PipelineState, TaskStatus
from litehive.domain.task import TaskRecord
from litehive.git.ops import current_head
from litehive.state.persist import load_state, save_state
from litehive.state.records import (
    get_task,
    get_task_worktree_path,
    save_task,
    set_task_commit_sha,
)
from litehive.worktree.cleanup import cleanup_terminal_task_worktree
from litehive.worktree.paths import resolve_recorded_worktree_path
from litehive.worktree.service import WorktreeService

from .nodes.system import CommitNode, GitCommitNode, GitWorktreeSyncNode
from .persistence import SqlitePersistence, TaskNotFound, TaskState
from .runtime_sync import _MANUAL_REVIEW_FLAG_REASONS


def _resolve_worktree(root: Path, state: TaskState) -> Path:
    """Look up the on-disk worktree path for a task, falling back to the workspace root when no worktree was recorded — the commit/sync nodes never want a missing path."""
    _, worktree_path = _task_recorded_worktree(root, state.task_id)
    return worktree_path or root


def _resolve_hook_execution_root(root: Path, state: TaskState) -> Path:
    """
    Pick the cwd for runner hooks based on stage.

    Pre-commit hooks run in the task worktree so they see the agent's
    edits; the ``after_commit`` hook runs on main because by that
    point the changes have already landed there and we want to verify
    the integrated state.
    """
    if state.stage == PipelineState.AFTER_COMMIT:
        return root
    return _resolve_worktree(root, state)


def _task_recorded_worktree(root: Path, task_id: str) -> tuple[TaskRecord | None, Path | None]:
    """
    Look up the task and its on-disk worktree path together.

    Commit and sync nodes need both the TaskRecord (for commit message
    rendering) and the worktree path (for the actual git work);
    returning them in one call means a single db hit per resolution
    instead of two.
    """
    task = get_task(root, task_id)
    if task is None:
        return None, None
    recorded = get_task_worktree_path(task)
    if not recorded:
        return task, None
    return task, resolve_recorded_worktree_path(root, recorded)


def build_commit_node(root: Path) -> CommitNode:
    """Return the production ``GitCommitNode`` bound to this workspace; called by ``orchestration.run_task`` once per launch to wire the commit stage."""
    return GitCommitNode(
        root,
        worktree_resolver=lambda state: _resolve_worktree(root, state),
        task_resolver=lambda state: _task_recorded_worktree(root, state.task_id)[0],
    )


def _build_worktree_sync_node(root: Path) -> GitWorktreeSyncNode:
    """Return the production ``GitWorktreeSyncNode`` bound to this workspace; mirrors ``build_commit_node`` for the worktree-sync stage."""
    return GitWorktreeSyncNode(
        workspace_root=root,
        worktree_resolver=lambda state: _resolve_worktree(root, state),
    )


def _worktree_missing_probe(root: Path):
    """
    Build the worktree-existence probe for ``ReadyNode``.

    Returns a closure that asks ``WorktreeService`` whether the task
    has a recorded worktree that is no longer on disk; the runner
    uses it to decide "should this task re-run worktree setup before
    launch?" without every call site reaching into the service
    directly.
    """
    service = WorktreeService(root)

    def _probe(state) -> bool:
        return service.task_has_missing_recorded_worktree(state.task_id)

    return _probe


def _worktree_metadata_repair(root: Path):
    """
    Build the stale-worktree-metadata repair for ``PreExecRecoveryNode``.

    Twin of ``_worktree_missing_probe``: when the probe says a
    recorded worktree is gone, the machine calls this to wipe the
    stale path on the task record so the next launch creates a fresh
    worktree instead of failing the existence check.
    """
    service = WorktreeService(root)

    def _repair(state) -> None:
        service.clear_missing_recorded_worktree(state.task_id)

    return _repair


def _mark_task_interrupted_on_crash(root: Path, task: TaskRecord) -> None:
    """Best-effort cleanup when run_task raises an unexpected exception.

    Clears active_task_id and marks the task as interrupted so the next
    runner start can resume it instead of finding stale "running" state.
    """
    try:
        state = load_state(root)
        if state.active_task_id == task.id:
            state.active_task_id = None
            if task.id not in state.queue:
                state.queue.insert(0, task.id)
            save_state(root, state)
        fresh = get_task(root, task.id)
        if fresh is not None and fresh.runtime.pipeline.execution_status == "running":
            fresh.runtime.pipeline.execution_status = "interrupted"
            fresh.status = TaskStatus.QUEUED
            save_task(root, fresh)
    except Exception:
        pass  # best-effort — don't mask the original crash


def _cleanup_terminal_worktree(root: Path, task: TaskRecord | None) -> None:
    """Tear down a finished task's worktree, but preserve worktrees for tasks flagged for manual review so an operator can inspect them."""
    if task is None:
        return
    fresh = get_task(root, task.id)
    if fresh is not None:
        task = fresh
    if task.status == TaskStatus.FLAGGED and task.flag_reason in _MANUAL_REVIEW_FLAG_REASONS:
        return
    cleanup_terminal_task_worktree(root, task)


def reconcile_terminal_commit_sha(
    root: Path,
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

    head_sha = commit_result.head_sha or current_head(root)
    if not head_sha:
        return task

    set_task_commit_sha(task, head_sha)
    save_task(root, task)
    return task
