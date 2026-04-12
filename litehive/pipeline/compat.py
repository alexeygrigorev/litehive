"""Compatibility helpers for the legacy package-level pipeline surface."""

from pathlib import Path

from litehive.attention import list_attention
from litehive.config.pool_types import (
    ExecutionSummary,
    RunResult,
    TaskPoolRunSummary,
    TaskPoolStopConditions,
)
from litehive.tasks.queue_ops import dequeue_next_task_selection, peek_next_task_selection
from litehive.workspace.locking import workspace_runner_guard
from litehive.workspace.worktree_inspection import git_worktree_blocks_pool

from .orchestration import run_task


def drain_task_pool(
    root: Path,
    *,
    engine_factory=None,
    stop_conditions: TaskPoolStopConditions | None = None,
) -> TaskPoolRunSummary:
    root = root.resolve()
    conditions = stop_conditions or TaskPoolStopConditions()
    executions: list[ExecutionSummary] = []

    with workspace_runner_guard(root):
        while True:
            stop_reason = _pool_stop_reason(root, executions, conditions)
            if stop_reason is not None:
                blocked = peek_next_task_selection(root).blocked if stop_reason == "blocked_tasks_remaining" else []
                return TaskPoolRunSummary(executions=executions, stop_reason=stop_reason, blocked=blocked)

            selection = dequeue_next_task_selection(root)
            if selection.task is None:
                stop_reason = "blocked_tasks_remaining" if selection.blocked else "queue_exhausted"
                return TaskPoolRunSummary(
                    executions=executions,
                    stop_reason=stop_reason,
                    blocked=selection.blocked,
                )

            result = run_task(root, selection.task, engine_factory=engine_factory)
            executions.append(
                ExecutionSummary(
                    task=result.task,
                    result=RunResult(
                        final_status=result.final_stage,
                        last_verdict="pass" if result.final_stage == "done" else "fail",
                    ),
                )
            )
            if conditions.stop_on_failure and result.final_stage != "done":
                return TaskPoolRunSummary(
                    executions=executions,
                    stop_reason="failure_detected",
                    blocked=[],
                )


def _pool_stop_reason(
    root: Path,
    executions: list[ExecutionSummary],
    conditions: TaskPoolStopConditions,
) -> str | None:
    if conditions.stop_on_dirty_git and git_worktree_blocks_pool(root):
        return "dirty_git_state"
    if conditions.stop_on_attention and list_attention(root):
        return "attention_required"
    if conditions.max_tasks is not None and len(executions) >= conditions.max_tasks:
        return "max_tasks_reached"
    return _blocked_only_stop_reason(root)


def _blocked_only_stop_reason(root: Path) -> str | None:
    selection = peek_next_task_selection(root)
    if selection.task is None and selection.blocked:
        return "blocked_tasks_remaining"
    return None
