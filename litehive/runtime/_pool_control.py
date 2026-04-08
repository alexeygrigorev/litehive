"""Pool stop-reason logic and finalization."""

from pathlib import Path

from litehive.tasks import (
    BlockedTask,
    append_journal,
    load_state,
    peek_next_task_selection,
    restore_untouched_active_task,
    set_pool_stop_reason,
)

from ._budget import (
    _count_execution_limits,
    _execution_exhausted_limit_fallbacks,
    _execution_hit_limit,
    _execution_limit_kind,
    _limit_stop_condition_is_configured,
)
from ._types import (
    EngineBudgetLedger,
    ExecutionSummary,
    TaskPoolRunSummary,
    TaskPoolStopConditions,
)
from ._worktree import _git_worktree_blocks_pool


def _pool_stop_reason(
    root: Path,
    executions: list[ExecutionSummary],
    conditions: TaskPoolStopConditions,
    *,
    budget_ledger: EngineBudgetLedger | None = None,
) -> str | None:
    if conditions.stop_on_dirty_git and _git_worktree_blocks_pool(root):
        return "dirty_git_state"
    if conditions.max_tasks is not None and len(executions) >= conditions.max_tasks:
        return "max_tasks_reached"
    if budget_ledger is not None:
        budget_stop_reason = budget_ledger.pool_stop_reason()
        if budget_stop_reason is not None:
            return budget_stop_reason
    if not executions:
        return None

    latest = executions[-1]
    if latest.result is not None and latest.result.final_status == "paused":
        return _human_checkpoint_stop_reason(latest)
    if latest.result is not None and latest.result.final_status == "queued":
        if conditions.stop_on_failure:
            return "failure_detected"
        selection = peek_next_task_selection(root)
        if selection.task is not None:
            return None
        return "blocked_tasks_remaining" if selection.blocked else "queue_exhausted"
    if latest.result is not None and latest.result.final_status == "interrupted":
        return "task_interrupted"
    if _requires_continue_or_rollback(root, latest):
        return "continue_or_rollback_required"
    latest_limit_kind = _execution_limit_kind(latest)
    final_status = latest.result.final_status if latest.result is not None else None
    if (
        conditions.stop_on_failure
        and final_status is not None
        and final_status not in {"done", "paused"}
    ):
        return "failure_detected"
    if conditions.stop_on_execution_limit and _execution_hit_limit(latest):
        return "execution_limit_reached"
    if (
        latest_limit_kind == "quota"
        and conditions.quota_threshold is not None
        and _count_execution_limits(executions, kind="quota") >= conditions.quota_threshold
    ):
        return "quota_threshold_reached"
    if (
        latest_limit_kind == "budget"
        and conditions.budget_threshold is not None
        and _count_execution_limits(executions, kind="budget") >= conditions.budget_threshold
    ):
        return "budget_threshold_reached"
    if _execution_exhausted_limit_fallbacks(latest) and not _limit_stop_condition_is_configured(
        conditions,
        latest_limit_kind,
    ):
        return "execution_limit_fallbacks_exhausted"
    return None


def _single_task_pre_stop_reason(
    root: Path,
    *,
    stop_conditions: TaskPoolStopConditions,
    budget_ledger: EngineBudgetLedger,
) -> str | None:
    if stop_conditions.stop_on_dirty_git and _git_worktree_blocks_pool(root):
        return "dirty_git_state"
    return budget_ledger.pool_stop_reason()


def _single_task_stop_reason(execution: ExecutionSummary) -> str:
    result = execution.result
    if result is not None and result.final_status == "paused":
        return _human_checkpoint_stop_reason(execution)
    if result is not None and result.final_status == "queued":
        return "task_requeued"
    if result is not None and result.final_status == "interrupted":
        return "task_interrupted"
    return "single_task_complete"


def _requires_continue_or_rollback(root: Path, execution: ExecutionSummary) -> bool:
    task = execution.task
    result = execution.result
    if task is None or result is None:
        return False
    if result.final_status != "done":
        return False
    if execution.commit_sha is None:
        return False
    state = load_state(root)
    return bool(state.queue)


def _finalize_pool_run(
    root: Path,
    *,
    executions: list[ExecutionSummary],
    stop_reason: str,
    blocked: list[BlockedTask],
) -> TaskPoolRunSummary:
    restore_untouched_active_task(root)
    set_pool_stop_reason(root, stop_reason)
    if stop_reason == "execution_limit_fallbacks_exhausted" and executions:
        latest = executions[-1]
        if latest.task is not None:
            outcome_reason = latest.task.runtime.last_outcome.reason or "engine fallbacks exhausted"
            append_journal(root, latest.task, f"Pool stopped: {stop_reason}. {outcome_reason}")
    if stop_reason.startswith("human_checkpoint_") and executions:
        latest = executions[-1]
        if latest.task is not None:
            checkpoint = stop_reason.removeprefix("human_checkpoint_").replace("_", " ")
            append_journal(
                root,
                latest.task,
                f"Pool stopped: {stop_reason}. Awaiting human review at {checkpoint}.",
            )
    if stop_reason == "continue_or_rollback_required" and executions:
        latest = executions[-1]
        if latest.task is not None and latest.commit_sha is not None:
            append_journal(
                root,
                latest.task,
                (
                    "Pool stopped: continue_or_rollback_required. "
                    "This task finished with checkpoint commit "
                    f"`{latest.commit_sha}` and unrelated queued work remains. "
                    "Either continue with a new `litehive run`/pool run or roll back the checkpoint first."
                ),
            )
    return TaskPoolRunSummary(executions=executions, stop_reason=stop_reason, blocked=blocked)


def _human_checkpoint_stop_reason(execution: ExecutionSummary) -> str:
    task = execution.task
    if task is None:
        return "human_checkpoint_reached"
    if task.pipeline_status == "accepting":
        return "human_checkpoint_before_acceptance"
    if task.pipeline_status == "commit_to_git":
        return "human_checkpoint_before_commit"
    return "human_checkpoint_reached"
