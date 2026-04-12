from pathlib import Path

from litehive.cli.display import cli_override_or_default
from litehive.cli.dry_run import (
    plan_pool_dry_run,
    plan_single_task_dry_run,
    print_pool_dry_run_plan,
)
from litehive.config import ensure_workspace, load_config
from litehive.pipeline.orchestration import run_task
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
    result = run_task(workspace, task)
    if result.task is not None:
        print(f"task: {result.task.id} {result.task.title}")
    print(f"final_stage: {result.final_stage}")
    if result.failed_reason:
        print(f"failed_reason: {result.failed_reason}")
    if result.failed_message:
        print(f"failed_message: {result.failed_message}")
    return 0 if result.final_stage == "done" else 1


def cmd_run(args):
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
    from litehive.config.pool_types import TaskPoolStopConditions

    stop_conditions = TaskPoolStopConditions(
        max_tasks=getattr(args, "max_tasks", None),
        stop_on_failure=cli_override_or_default(
            getattr(args, "stop_on_failure", None), config.pool_stop_on_failure
        ),
        stop_on_dirty_git=cli_override_or_default(
            getattr(args, "stop_on_dirty_git", None), config.pool_stop_on_dirty_git
        ),
        stop_on_attention=config.pool_stop_on_attention,
    )
    runnable_tasks, predicted_stop_reason = plan_pool_dry_run(
        args.workspace,
        planned_tasks=plan.tasks,
        blocked_count=len(plan.blocked),
        config=config,
        stop_conditions=stop_conditions,
        engine_override=engine_override,
        model_override=model_override,
    )
    print_pool_dry_run_plan(
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
    from litehive.config.pool_types import TaskPoolStopConditions

    stop_conditions = TaskPoolStopConditions(
        max_tasks=getattr(args, "max_tasks", None),
        stop_on_failure=cli_override_or_default(
            getattr(args, "stop_on_failure", None), config.pool_stop_on_failure
        ),
        stop_on_dirty_git=cli_override_or_default(
            getattr(args, "stop_on_dirty_git", None), config.pool_stop_on_dirty_git
        ),
        stop_on_attention=config.pool_stop_on_attention,
    )
    planned_tasks = [selection.task] if selection.task is not None else []
    runnable_tasks, predicted_stop_reason = plan_single_task_dry_run(
        args.workspace,
        planned_tasks=planned_tasks,
        blocked_count=len(selection.blocked),
        config=config,
        stop_conditions=stop_conditions,
        engine_override=engine_override,
        model_override=model_override,
    )
    print_pool_dry_run_plan(
        args.workspace,
        planned_tasks=runnable_tasks,
        blocked=selection.blocked,
        config=config,
        stop_conditions=stop_conditions,
        predicted_stop_reason=predicted_stop_reason,
    )
    return 0
