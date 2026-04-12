from litehive.config.engine_models import select_engine
from litehive.config.pool_types import EngineBudgetLedger

from litehive.cli._pool import _pool_stop_condition_label
from litehive.cli._display import _format_engine_int_map


def _determine_dry_run_stop_reason(
    blocked_reasons,
    *,
    stop_conditions,
):
    combined = " ".join(reason.lower() for reason in blocked_reasons)
    if "pool usage cap reached" in combined:
        return "pool_usage_cap_reached"
    if "pool cost cap reached" in combined:
        return "pool_cost_cap_reached"
    if stop_conditions.stop_on_execution_limit:
        return "execution_limit_reached"
    return "execution_limit_fallbacks_exhausted"


def _plan_pool_dry_run(
    root,
    *,
    planned_tasks,
    blocked_count,
    config,
    stop_conditions,
    engine_override,
    model_override,
):
    from litehive.workspace.worktree_inspection import _git_worktree_blocks_pool

    if stop_conditions.stop_on_dirty_git and _git_worktree_blocks_pool(root):
        return [], "dirty_git_state"

    budget_ledger = EngineBudgetLedger(
        pool_usage_cap=stop_conditions.pool_usage_cap,
        pool_cost_cap=stop_conditions.pool_cost_cap,
        engine_usage_caps=dict(stop_conditions.engine_usage_caps),
        engine_budget_caps=dict(stop_conditions.engine_budget_caps),
        engine_costs=dict(stop_conditions.engine_costs),
    )
    runnable_tasks = []

    for task in planned_tasks:
        if (
            stop_conditions.max_tasks is not None
            and len(runnable_tasks) >= stop_conditions.max_tasks
        ):
            return runnable_tasks, "max_tasks_reached"
        pool_stop_reason = budget_ledger.pool_stop_reason()
        if pool_stop_reason is not None:
            return runnable_tasks, pool_stop_reason

        selection = select_engine(
            root,
            task,
            config,
            budget_ledger=budget_ledger,
            engine_override=engine_override,
            model_override=model_override,
        )
        if selection.engine_name is None:
            return runnable_tasks, _determine_dry_run_stop_reason(
                [skip.reason for skip in selection.skipped] or [selection.blocked_reason or ""],
                stop_conditions=stop_conditions,
            )
        runnable_tasks.append(
            (
                task,
                selection.engine_name,
                selection.engine_attempts,
                selection.model_name,
            )
        )
        budget_ledger.record(selection.engine_name)

    pool_stop_reason = budget_ledger.pool_stop_reason()
    if pool_stop_reason is not None:
        return runnable_tasks, pool_stop_reason
    if blocked_count:
        return runnable_tasks, "blocked_tasks_remaining"
    return runnable_tasks, "queue_exhausted"


def _plan_single_task_dry_run(
    root,
    *,
    planned_tasks,
    blocked_count,
    config,
    stop_conditions,
    engine_override,
    model_override,
):
    from litehive.workspace.worktree_inspection import _git_worktree_blocks_pool

    if stop_conditions.stop_on_dirty_git and _git_worktree_blocks_pool(root):
        return [], "dirty_git_state"
    if not planned_tasks:
        if blocked_count:
            return [], "blocked_tasks_remaining"
        return [], "queue_exhausted"

    budget_ledger = _budget_ledger_from_stop_conditions(stop_conditions)
    task = planned_tasks[0]
    selection = select_engine(
        root,
        task,
        config,
        budget_ledger=budget_ledger,
        engine_override=engine_override,
        model_override=model_override,
    )
    if selection.engine_name is None:
        return [], _determine_dry_run_stop_reason(
            [skip.reason for skip in selection.skipped] or [selection.blocked_reason or ""],
            stop_conditions=stop_conditions,
        )
    return [
        (
            task,
            selection.engine_name,
            selection.engine_attempts,
            selection.model_name,
        )
    ], "single_task_complete"


def _print_pool_dry_run_plan(
    root,
    *,
    planned_tasks,
    blocked,
    config,
    stop_conditions,
    predicted_stop_reason,
):
    print("dry_run: true")
    print(f"selection_policy: {config.pool_selection_policy}")
    print(f"planned_tasks: {len(planned_tasks)}")
    for index, (task, selected_engine, engine_attempts, selected_model) in enumerate(
        planned_tasks, start=1
    ):
        checkpoints = ", ".join(task.human_checkpoints) if task.human_checkpoints else "-"
        model_label = selected_model or "-"
        print(
            f"would_run: {index}. {task.id} {task.title} "
            f"status={task.status} pipeline_status={task.pipeline_status} "
            f"engine={selected_engine} engine_attempts={', '.join(engine_attempts)} "
            f"model={model_label} human_checkpoints={checkpoints}"
        )
    print(f"blocked_tasks: {len(blocked)}")
    for blocked_task in blocked:
        print(
            f"blocked: {blocked_task.task_id} {blocked_task.title} "
            f"blocked_by={', '.join(blocked_task.blocked_by)}"
        )
    print(f"predicted_stop_condition: {_pool_stop_condition_label(predicted_stop_reason)}")
    print(f"predicted_stop_reason: {predicted_stop_reason}")
    print(f"stop_on_failure: {stop_conditions.stop_on_failure}")
    print(f"max_tasks: {stop_conditions.max_tasks}")
    print(f"stop_on_execution_limit: {stop_conditions.stop_on_execution_limit}")
    print(f"quota_threshold: {stop_conditions.quota_threshold}")
    print(f"budget_threshold: {stop_conditions.budget_threshold}")
    print(f"pool_usage_cap: {stop_conditions.pool_usage_cap}")
    print(f"pool_cost_cap: {stop_conditions.pool_cost_cap}")
    print(f"engine_usage_caps: {_format_engine_int_map(stop_conditions.engine_usage_caps)}")
    print(f"engine_budget_caps: {_format_engine_int_map(stop_conditions.engine_budget_caps)}")
    print(f"engine_costs: {_format_engine_int_map(stop_conditions.engine_costs)}")
    print(f"stop_on_dirty_git: {stop_conditions.stop_on_dirty_git}")


def _budget_ledger_from_stop_conditions(
    stop_conditions,
):
    return EngineBudgetLedger(
        pool_usage_cap=stop_conditions.pool_usage_cap,
        pool_cost_cap=stop_conditions.pool_cost_cap,
        engine_usage_caps=dict(stop_conditions.engine_usage_caps),
        engine_budget_caps=dict(stop_conditions.engine_budget_caps),
        engine_costs=dict(stop_conditions.engine_costs),
    )
