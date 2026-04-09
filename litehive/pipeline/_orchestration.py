"""Top-level task orchestration (resolve/run/drain)."""

from pathlib import Path
from typing import Callable

from litehive.config import load_config, load_context
from litehive.models import TaskRecord
from litehive.pipeline.core import TaskExecutionRunner
from litehive.agents import SubagentManager
from litehive.tasks.journal import append_journal
from litehive.tasks.models import BlockedTask, WorkspaceConflictError
from litehive.tasks.persistence import set_pool_stop_reason
from litehive.tasks.queue_ops import (
    active_task_markers,
    dequeue_next_task,
    dequeue_next_task_selection,
    peek_next_task,
    peek_next_task_selection,
)
from litehive.workspace.locking import runner_heartbeat, workspace_runner_guard
from litehive.pipeline.recovery import recover_stale_runner_state
from litehive.workspace.runtime_tracking import mark_task_run_started

from ._budget import _budget_ledger_from_conditions, _budget_ledger_from_config
from ._builder import build_executor
from ._models import (
    _resolve_stage_retry_limit,
    resolve_engine_plan,
    resolve_task_retry_policy,
)
from ._pool_control import (
    _finalize_pool_run,
    _pool_stop_reason,
    _single_task_pre_stop_reason,
    _single_task_stop_reason,
)
from ._types import (
    EngineBudgetLedger,
    ExecutionSummary,
    SingleTaskRunSummary,
    TaskPoolRunSummary,
    TaskPoolStopConditions,
)
from ._worktree import _resolve_task_execution_root


def resolve_next_task(root: Path) -> TaskRecord | None:
    root = root.resolve()
    return peek_next_task(root)


def run_next_task(root: Path) -> ExecutionSummary:
    root = root.resolve()
    task = dequeue_next_task(root)
    return run_task(root, task)


def run_task(
    root: Path,
    task: TaskRecord | None,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    budget_ledger: EngineBudgetLedger | None = None,
) -> ExecutionSummary:
    root = root.resolve()
    if task is None:
        return ExecutionSummary(task=None, result=None)
    recover_stale_runner_state(root)
    markers = active_task_markers(root)
    if markers:
        active_ids = sorted(markers)
        if len(active_ids) > 1:
            details = "; ".join(
                f"{task_id} ({', '.join(markers[task_id])})" for task_id in active_ids
            )
            raise WorkspaceConflictError(
                f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
            )
        active_id = active_ids[0]
        if active_id != task.id:
            raise WorkspaceConflictError(
                f"task {task.id} cannot start because task {active_id} is already active in this workspace."
            )
    with workspace_runner_guard(root):
        markers = active_task_markers(root)
        if markers:
            active_ids = sorted(markers)
            if len(active_ids) > 1:
                details = "; ".join(
                    f"{task_id} ({', '.join(markers[task_id])})" for task_id in active_ids
                )
                raise WorkspaceConflictError(
                    f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
                )
            active_id = active_ids[0]
            if active_id != task.id:
                raise WorkspaceConflictError(
                    f"task {task.id} cannot start because task {active_id} is already active in this workspace."
                )

        config = load_config(root)
        workspace_context = load_context(root)
        engine_plan = resolve_engine_plan(task, config, engine_override=engine_override)
        engine_name = engine_plan[0]
        execution_root = _resolve_task_execution_root(root, task, config=config)
        subagents = SubagentManager(root, execution_root=execution_root)

        append_journal(root, task, f"Execution started with engine `{engine_name}`.")
        mark_task_run_started(root, task)
        retry_limit, retry_source = resolve_task_retry_policy(task, config)

        runner = TaskExecutionRunner(
            root,
            build_executor(
                root,
                execution_root=execution_root,
                initial_engine_names=engine_plan,
                workspace_context=workspace_context,
                subagents=subagents,
                config=config,
                task=task,
                model_override=model_override,
                config_auto_commit=config.auto_commit,
                budget_ledger=budget_ledger or _budget_ledger_from_config(config),
            ),
            max_retries=retry_limit,
            retry_source=retry_source,
            stage_retry_limit=_resolve_stage_retry_limit(task, config),
            subagents=subagents,
            config=config,
        )
        with runner_heartbeat(root, active_task_id=task.id):
            result = runner.run(task)
        if result.final_status != "done":
            append_journal(root, task, f"Execution finished with status `{result.final_status}`.")

        return ExecutionSummary(task=task, result=result, commit_sha=task.git.commit_sha)


def run_single_task(
    root: Path,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    stop_conditions: TaskPoolStopConditions | None = None,
) -> SingleTaskRunSummary:
    root = root.resolve()
    with workspace_runner_guard(root):
        conditions = stop_conditions or TaskPoolStopConditions()
        budget_ledger = _budget_ledger_from_conditions(conditions)
        set_pool_stop_reason(root, None)

        stop_reason = _single_task_pre_stop_reason(
            root,
            stop_conditions=conditions,
            budget_ledger=budget_ledger,
        )
        if stop_reason is not None:
            set_pool_stop_reason(root, stop_reason)
            return SingleTaskRunSummary(execution=None, stop_reason=stop_reason, blocked=[])

        execution, blocked = run_next_task_with_override(
            root,
            engine_override=engine_override,
            model_override=model_override,
            budget_ledger=budget_ledger,
        )
        if execution.task is None:
            stop_reason = "blocked_tasks_remaining" if blocked else "queue_exhausted"
            set_pool_stop_reason(root, stop_reason)
            return SingleTaskRunSummary(execution=None, stop_reason=stop_reason, blocked=blocked)

        stop_reason = _single_task_stop_reason(execution)
        if stop_reason.startswith("human_checkpoint_") or stop_reason == "task_requeued":
            set_pool_stop_reason(root, stop_reason)
        return SingleTaskRunSummary(execution=execution, stop_reason=stop_reason, blocked=blocked)


def drain_task_pool(
    root: Path,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    stop_conditions: TaskPoolStopConditions | None = None,
    stop_when: Callable[[list[ExecutionSummary]], bool] | None = None,
) -> TaskPoolRunSummary:
    root = root.resolve()
    with workspace_runner_guard(root):
        executions: list[ExecutionSummary] = []
        conditions = stop_conditions or TaskPoolStopConditions()
        budget_ledger = _budget_ledger_from_conditions(conditions)
        set_pool_stop_reason(root, None)

        while True:
            stop_reason = _pool_stop_reason(
                root, executions, conditions, budget_ledger=budget_ledger
            )
            if stop_reason is not None:
                blocked: list[BlockedTask] = []
                if stop_reason == "blocked_tasks_remaining":
                    blocked = peek_next_task_selection(root).blocked
                return _finalize_pool_run(
                    root, executions=executions, stop_reason=stop_reason, blocked=blocked
                )
            if stop_when is not None and stop_when(executions):
                return _finalize_pool_run(
                    root,
                    executions=executions,
                    stop_reason="stop_condition_reached",
                    blocked=[],
                )
            execution, blocked = run_next_task_with_override(
                root,
                engine_override=engine_override,
                model_override=model_override,
                budget_ledger=budget_ledger,
            )
            if execution.task is None:
                stop_reason = "blocked_tasks_remaining" if blocked else "queue_exhausted"
                return _finalize_pool_run(
                    root, executions=executions, stop_reason=stop_reason, blocked=blocked
                )
            executions.append(execution)


def run_next_task_with_override(
    root: Path,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    budget_ledger: EngineBudgetLedger | None = None,
) -> tuple[ExecutionSummary, list[BlockedTask]]:
    root = root.resolve()
    selection = dequeue_next_task_selection(root)
    return (
        run_task(
            root,
            selection.task,
            engine_override=engine_override,
            model_override=model_override,
            budget_ledger=budget_ledger,
        ),
        selection.blocked,
    )
