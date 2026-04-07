from litehive.config import ensure_workspace, load_config
from litehive.observability import load_engine_monitoring, render_engine_monitoring_lines, render_task_summary
from litehive.runtime._models import active_engine_freezes
from litehive.tasks import (
    WorkspaceConflictError,
    list_tasks,
    list_tasks_state_first,
    load_state,
    recover_stale_runner_state,
    repair_workspace_state,
    require_task,
    runner_status,
)

from litehive.cli._display import (
    _format_execution_retry_policies,
    _task_dependencies_label,
    _task_engine_label,
    _task_interruption_label,
    _task_model_label,
)


def _cmd_status(args):
    root = args.workspace.resolve()
    config = load_config(args.workspace)
    state = load_state(args.workspace)
    monitoring = load_engine_monitoring(args.workspace)
    fast_mode = bool(getattr(args, "fast", False))
    print(f"workspace: {args.workspace}")
    print(f"status_read_mode: {'fast' if fast_mode else 'full'}")
    print(f"default_engine: {config.default_engine}")
    freezes = active_engine_freezes(config)
    if freezes:
        for engine_name, until_dt in sorted(freezes.items()):
            local_until = until_dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")
            print(f"engine_frozen: {engine_name} until {local_until}")
    print(f"litehive_source_path: {config.litehive_source_path or '-'}")
    print(f"mode: {state.mode}")
    print(f"active_task_id: {state.active_task_id}")
    current_runner = runner_status(root)
    print(
        "runner_status: "
        f"{current_runner.status} pid={current_runner.pid or '-'} "
        f"started_at={current_runner.started_at or '-'} "
        f"heartbeat_at={current_runner.heartbeat_at or '-'} "
        f"active_task_id={current_runner.active_task_id or '-'}"
    )
    print(f"queued_tasks: {len(state.queue)}")
    print(f"pool_stop_reason: {state.pool_stop_reason}")
    if state.queue:
        print(f"queue_head: {state.queue[0]}")
    active_task = (
        require_task(args.workspace, state.active_task_id) if state.active_task_id else None
    )
    if active_task is not None:
        active_engine = (
            active_task.runtime.active_subagent.engine
            if active_task.runtime.active_subagent is not None
            else active_task.runtime.last_subagent.engine
            if active_task.runtime.last_subagent is not None
            else active_task.engine or config.default_engine
        )
        active_stage = active_task.runtime.current_stage.step or active_task.pipeline_status or "-"
        print(f"active_task_title: {active_task.title}")
        print(f"active_task_status: {active_task.status}/{active_task.pipeline_status}")
        print(f"active_stage: {active_stage}")
        print(f"active_engine: {active_engine}")
    for line in render_engine_monitoring_lines(monitoring):
        print(line)
    if getattr(args, "full", True):
        print(f"default_retry_limit: {config.default_retry_limit}")
        print(f"execution_retry_policies: {_format_execution_retry_policies(config)}")
        print(f"pool_stop_on_failure: {config.pool_stop_on_failure}")
        print(f"pool_max_tasks: {config.pool_max_tasks}")
        print(f"pool_stop_on_execution_limit: {config.pool_stop_on_execution_limit}")
        print(f"pool_quota_threshold: {config.pool_quota_threshold}")
        print(f"pool_budget_threshold: {config.pool_budget_threshold}")
        print(f"pool_stop_on_dirty_git: {config.pool_stop_on_dirty_git}")
        print(f"pool_selection_policy: {config.pool_selection_policy}")
        print(f"process_profile: {config.process_profile}")
        tasks = (
            list_tasks_state_first(args.workspace, state=state)
            if fast_mode
            else list_tasks(args.workspace)
        )
        if tasks:
            print()
            for task in tasks:
                for line in render_task_summary(
                    task, active=task.id == state.active_task_id, root=root
                ):
                    print(line)
    return 0


def _cmd_queue(args):
    config = load_config(args.workspace)
    recover_stale_runner_state(args.workspace)
    state = load_state(args.workspace)
    tasks = list_tasks(args.workspace)
    print(f"active_task_id: {state.active_task_id}")
    if state.active_task_id is not None:
        active_task = require_task(args.workspace, state.active_task_id)
        print(
            f"active: {active_task.id} [{active_task.status}/{active_task.pipeline_status}] "
            f"priority={active_task.priority} engine={_task_engine_label(active_task.engine, config.default_engine)} "
            f"model={_task_model_label(active_task.model)} "
            f"title={active_task.title} depends_on={_task_dependencies_label(active_task.id, active_task.depends_on)}"
            f"{_task_interruption_label(active_task)}"
        )
    print(f"queue_length: {len(state.queue)}")
    for index, task_id in enumerate(state.queue, start=1):
        task = require_task(args.workspace, task_id)
        print(
            f"{index}. {task.id} [{task.status}/{task.pipeline_status}] "
            f"priority={task.priority} engine={_task_engine_label(task.engine, config.default_engine)} "
            f"model={_task_model_label(task.model)} "
            f"title={task.title} depends_on={_task_dependencies_label(task.id, task.depends_on)}"
            f"{_task_interruption_label(task)}"
        )
    resumable = [task for task in tasks if task.status in {"interrupted", "parked"}]
    print(f"resumable_tasks: {len(resumable)}")
    for index, task in enumerate(resumable, start=1):
        print(
            f"resume {index}. {task.id} [{task.status}/{task.pipeline_status}] "
            f"priority={task.priority} engine={_task_engine_label(task.engine, config.default_engine)} "
            f"model={_task_model_label(task.model)} "
            f"title={task.title} depends_on={_task_dependencies_label(task.id, task.depends_on)}"
            f"{_task_interruption_label(task)}"
        )
    return 0


def _cmd_list(args):
    ensure_workspace(args.workspace)
    tasks = list_tasks(args.workspace)
    show_all = getattr(args, "show_all", False)
    filter_status = getattr(args, "filter_status", None)
    filter_pipeline = getattr(args, "filter_pipeline_status", None)
    filter_engine = getattr(args, "filter_engine", None)

    filtered = []
    for task in tasks:
        if not show_all and task.status == "done":
            continue
        if filter_status and task.status != filter_status:
            continue
        if filter_pipeline and task.pipeline_status != filter_pipeline:
            continue
        if filter_engine and (task.engine or "") != filter_engine:
            continue
        filtered.append(task)

    for task in filtered:
        print(f"{task.id} [{task.status}/{task.pipeline_status}] {task.title}")
    return 0


def _cmd_show(args):
    ensure_workspace(args.workspace)
    from litehive.tasks import get_task

    task = get_task(args.workspace, args.task_id)
    if task is None:
        print(f"error: task {args.task_id} not found")
        return 1

    print(f"id: {task.id}")
    print(f"slug: {task.slug}")
    print(f"title: {task.title}")
    print(f"status: {task.status}")
    print(f"pipeline_status: {task.pipeline_status}")
    print(f"priority: {task.priority}")
    print(f"engine: {task.engine or '-'}")
    print(f"model: {task.model or '-'}")
    print(f"task_type: {task.task_type or '-'}")
    print(f"mode: {task.mode}")
    print(f"pipeline_mode: {task.pipeline_mode}")
    print(f"pm_complexity: {task.pm_complexity or '-'}")
    print(f"planned_effort: {task.planned_effort or '-'}")
    print(f"created_at: {task.created_at}")
    print(f"updated_at: {task.updated_at}")
    if task.depends_on:
        print(f"depends_on: {', '.join(task.depends_on)}")
    else:
        print("depends_on: -")
    print(f"goal: {task.goal or '-'}")
    if task.acceptance_criteria:
        print("acceptance_criteria:")
        for ac in task.acceptance_criteria:
            print(f"  - {ac}")
    else:
        print("acceptance_criteria: -")
    if task.constraints:
        print("constraints:")
        for c in task.constraints:
            print(f"  - {c}")
    else:
        print("constraints: -")
    if task.plan:
        print("plan:")
        for step in task.plan:
            print(f"  - {step}")
    else:
        print("plan: -")
    # Runtime info
    rt = task.runtime
    print(f"execution_status: {rt.execution_status or '-'}")
    print(f"current_stage: {rt.current_stage.step if rt.current_stage and rt.current_stage.step else '-'}")
    print(f"retry_count: {rt.retry_count}")
    print(f"last_outcome: {rt.last_outcome or '-'}")
    return 0


def _cmd_repair(args):
    ensure_workspace(args.workspace)
    try:
        summary = repair_workspace_state(args.workspace)
    except WorkspaceConflictError as exc:
        print(f"repair failed: {exc}")
        return 1
    state = load_state(args.workspace)
    print(f"repaired: {'yes' if summary.mutated else 'no'}")
    print(f"stale_runner_recovered: {'yes' if summary.stale_runner_recovered else 'no'}")
    print(f"cleared_active_task_id: {summary.cleared_active_task_id or '-'}")
    print(
        "requeued_tasks: "
        + (" ".join(summary.requeued_task_ids) if summary.requeued_task_ids else "-")
    )
    print(
        "removed_queue_entries: "
        + (" ".join(summary.removed_queue_entries) if summary.removed_queue_entries else "-")
    )
    print(
        "deduped_queue_entries: "
        + (" ".join(summary.deduped_queue_entries) if summary.deduped_queue_entries else "-")
    )
    print(
        "restored_queue_entries: "
        + (" ".join(summary.restored_queue_entries) if summary.restored_queue_entries else "-")
    )
    print(
        "finalized_commit_tasks: "
        + (
            " ".join(summary.finalized_commit_task_ids)
            if summary.finalized_commit_task_ids
            else "-"
        )
    )
    print(
        "stale_process_tasks: "
        + (" ".join(summary.stale_process_task_ids) if summary.stale_process_task_ids else "-")
    )
    print(f"active_task_id: {state.active_task_id}")
    print(f"queue_length: {len(state.queue)}")
    return 0
