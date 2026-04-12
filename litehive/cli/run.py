from pathlib import Path

from litehive.cli._display import _cli_override_or_default
from litehive.cli._dry_run import (
    _plan_pool_dry_run,
    _plan_single_task_dry_run,
    _print_pool_dry_run_plan,
)
from litehive.cli._parse import _parse_engine_int_map
from litehive.config import ensure_workspace, load_config
from litehive.pipeline.orchestration import run_task_v2
from litehive.tasks.models import WorkspaceConflictError
from litehive.tasks.queue_ops import dequeue_next_task, peek_next_task_selection, plan_task_selections


def _run_single_v2(workspace: Path) -> int:
    """Dequeue one task and run it through the v2 state machine."""
    try:
        task = dequeue_next_task(workspace)
    except WorkspaceConflictError as exc:
        print(f"run failed: {exc}")
        return 1
    if task is None:
        print("No queued task.")
        return 0
    result = run_task_v2(workspace, task)
    if result.task is not None:
        print(f"task: {result.task.id} {result.task.title}")
    print(f"final_stage: {result.final_stage}")
    if result.failed_reason:
        print(f"failed_reason: {result.failed_reason}")
    if result.failed_message:
        print(f"failed_message: {result.failed_message}")
    return 0 if result.final_stage == "done" else 1


def _cmd_run(args):
    ensure_workspace(args.workspace)

    if args.dry_run:
        config = load_config(args.workspace)
        if bool(getattr(args, "drain", False)):
            return _cmd_run_drain_dry_run(args, config=config)
        return _cmd_run_single_dry_run(args, config=config)

    return _run_single_v2(args.workspace)


def _cmd_run_drain_dry_run(args, *, config):
    try:
        plan = plan_task_selections(args.workspace)
    except WorkspaceConflictError as exc:
        print(f"run failed: {exc}")
        return 1

    engine_override = getattr(args, "engine", None)
    model_override = getattr(args, "model", None)
    engine_usage_caps = _cli_override_or_default(
        _parse_engine_int_map(
            getattr(args, "engine_usage_cap", None), option_name="--engine-usage-cap"
        )
        if getattr(args, "engine_usage_cap", None) is not None
        else None,
        config.engine_usage_caps,
    )
    engine_budget_caps = _cli_override_or_default(
        _parse_engine_int_map(
            getattr(args, "engine_budget_cap", None), option_name="--engine-budget-cap"
        )
        if getattr(args, "engine_budget_cap", None) is not None
        else None,
        config.engine_budget_caps,
    )
    engine_costs = _cli_override_or_default(
        _parse_engine_int_map(
            getattr(args, "engine_cost", None), option_name="--engine-cost"
        )
        if getattr(args, "engine_cost", None) is not None
        else None,
        config.engine_costs,
    )
    from litehive.pipeline_old import TaskPoolStopConditions

    stop_conditions = TaskPoolStopConditions(
        max_tasks=getattr(args, "max_tasks", None),
        stop_on_failure=_cli_override_or_default(
            getattr(args, "stop_on_failure", None), config.pool_stop_on_failure
        ),
        stop_on_execution_limit=_cli_override_or_default(
            getattr(args, "stop_on_execution_limit", None),
            config.pool_stop_on_execution_limit,
        ),
        quota_threshold=_cli_override_or_default(
            getattr(args, "quota_threshold", None), config.pool_quota_threshold
        ),
        budget_threshold=_cli_override_or_default(
            getattr(args, "budget_threshold", None), config.pool_budget_threshold
        ),
        pool_usage_cap=_cli_override_or_default(
            getattr(args, "pool_usage_cap", None), config.pool_usage_cap
        ),
        pool_cost_cap=_cli_override_or_default(
            getattr(args, "pool_cost_cap", None), config.pool_cost_cap
        ),
        engine_usage_caps=engine_usage_caps,
        engine_budget_caps=engine_budget_caps,
        engine_costs=engine_costs,
        stop_on_dirty_git=_cli_override_or_default(
            getattr(args, "stop_on_dirty_git", None), config.pool_stop_on_dirty_git
        ),
    )
    runnable_tasks, predicted_stop_reason = _plan_pool_dry_run(
        args.workspace,
        planned_tasks=plan.tasks,
        blocked_count=len(plan.blocked),
        config=config,
        stop_conditions=stop_conditions,
        engine_override=engine_override,
        model_override=model_override,
    )
    _print_pool_dry_run_plan(
        args.workspace,
        planned_tasks=runnable_tasks,
        blocked=plan.blocked,
        config=config,
        stop_conditions=stop_conditions,
        predicted_stop_reason=predicted_stop_reason,
    )
    return 0


def _cmd_run_single_dry_run(args, *, config):
    try:
        selection = peek_next_task_selection(args.workspace)
    except WorkspaceConflictError as exc:
        print(f"run failed: {exc}")
        return 1

    engine_override = getattr(args, "engine", None)
    model_override = getattr(args, "model", None)
    engine_usage_caps = _cli_override_or_default(
        _parse_engine_int_map(
            getattr(args, "engine_usage_cap", None), option_name="--engine-usage-cap"
        )
        if getattr(args, "engine_usage_cap", None) is not None
        else None,
        config.engine_usage_caps,
    )
    engine_budget_caps = _cli_override_or_default(
        _parse_engine_int_map(
            getattr(args, "engine_budget_cap", None), option_name="--engine-budget-cap"
        )
        if getattr(args, "engine_budget_cap", None) is not None
        else None,
        config.engine_budget_caps,
    )
    engine_costs = _cli_override_or_default(
        _parse_engine_int_map(
            getattr(args, "engine_cost", None), option_name="--engine-cost"
        )
        if getattr(args, "engine_cost", None) is not None
        else None,
        config.engine_costs,
    )
    from litehive.pipeline_old import TaskPoolStopConditions

    stop_conditions = TaskPoolStopConditions(
        max_tasks=getattr(args, "max_tasks", None),
        stop_on_failure=_cli_override_or_default(
            getattr(args, "stop_on_failure", None), config.pool_stop_on_failure
        ),
        stop_on_execution_limit=_cli_override_or_default(
            getattr(args, "stop_on_execution_limit", None),
            config.pool_stop_on_execution_limit,
        ),
        quota_threshold=_cli_override_or_default(
            getattr(args, "quota_threshold", None), config.pool_quota_threshold
        ),
        budget_threshold=_cli_override_or_default(
            getattr(args, "budget_threshold", None), config.pool_budget_threshold
        ),
        pool_usage_cap=_cli_override_or_default(
            getattr(args, "pool_usage_cap", None), config.pool_usage_cap
        ),
        pool_cost_cap=_cli_override_or_default(
            getattr(args, "pool_cost_cap", None), config.pool_cost_cap
        ),
        engine_usage_caps=engine_usage_caps,
        engine_budget_caps=engine_budget_caps,
        engine_costs=engine_costs,
        stop_on_dirty_git=_cli_override_or_default(
            getattr(args, "stop_on_dirty_git", None), config.pool_stop_on_dirty_git
        ),
    )
    planned_tasks = [selection.task] if selection.task is not None else []
    runnable_tasks, predicted_stop_reason = _plan_single_task_dry_run(
        args.workspace,
        planned_tasks=planned_tasks,
        blocked_count=len(selection.blocked),
        config=config,
        stop_conditions=stop_conditions,
        engine_override=engine_override,
        model_override=model_override,
    )
    _print_pool_dry_run_plan(
        args.workspace,
        planned_tasks=runnable_tasks,
        blocked=selection.blocked,
        config=config,
        stop_conditions=stop_conditions,
        predicted_stop_reason=predicted_stop_reason,
    )
    return 0
