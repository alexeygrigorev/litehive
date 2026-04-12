from litehive.config.engine_models import select_engine

from litehive.cli.pool import pool_stop_condition_label


def _determine_dry_run_stop_reason(
    blocked_reasons,
    *,
    stop_conditions,
):
    return "execution_limit_fallbacks_exhausted"


def plan_pool_dry_run(
    root,
    *,
    planned_tasks,
    blocked_count,
    config,
    stop_conditions,
    engine_override,
    model_override,
):
    from litehive.workspace.worktree_inspection import git_worktree_blocks_pool

    if stop_conditions.stop_on_dirty_git and git_worktree_blocks_pool(root):
        return [], "dirty_git_state"
    runnable_tasks = []

    for task in planned_tasks:
        if (
            stop_conditions.max_tasks is not None
            and len(runnable_tasks) >= stop_conditions.max_tasks
        ):
            return runnable_tasks, "max_tasks_reached"

        selection = select_engine(
            root,
            task,
            config,
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
    if blocked_count:
        return runnable_tasks, "blocked_tasks_remaining"
    return runnable_tasks, "queue_exhausted"


def plan_single_task_dry_run(
    root,
    *,
    planned_tasks,
    blocked_count,
    config,
    stop_conditions,
    engine_override,
    model_override,
):
    from litehive.workspace.worktree_inspection import git_worktree_blocks_pool

    if stop_conditions.stop_on_dirty_git and git_worktree_blocks_pool(root):
        return [], "dirty_git_state"
    if not planned_tasks:
        if blocked_count:
            return [], "blocked_tasks_remaining"
        return [], "queue_exhausted"

    task = planned_tasks[0]
    selection = select_engine(
        root,
        task,
        config,
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


def print_pool_dry_run_plan(
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
    print(f"predicted_stop_condition: {pool_stop_condition_label(predicted_stop_reason)}")
    print(f"predicted_stop_reason: {predicted_stop_reason}")
    print(f"stop_on_failure: {stop_conditions.stop_on_failure}")
    print(f"max_tasks: {stop_conditions.max_tasks}")
    print(f"stop_on_dirty_git: {stop_conditions.stop_on_dirty_git}")
