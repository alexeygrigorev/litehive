import csv

from litehive.config import ensure_workspace, load_config
from litehive.observability import (
    collect_recent_activity,
    find_last_completed_task,
    load_engine_monitoring,
    render_active_task_section,
    render_engine_health_section,
    render_engine_monitoring_lines,
    render_last_completed_section,
    render_queue_section,
    render_recent_activity_section,
    render_task_summary,
)
from litehive.pipeline._models import active_engine_freezes
from litehive.tasks.archive import archive_root
from litehive.tasks.crud import list_tasks, list_tasks_state_first, require_task
from litehive.tasks.models import WorkspaceConflictError
from litehive.tasks.persistence import load_state
from litehive.workspace.locking import runner_status
from litehive.workspace.recovery import recover_stale_runner_state, repair_workspace_state

from litehive.cli._display import (
    _format_execution_retry_policies,
    _task_dependencies_label,
    _task_engine_label,
    _task_interruption_label,
    _task_model_label,
)


def _display_flag_reason(task) -> str:
    if task.status != "flagged":
        return "-"
    return task.flag_reason or "unknown"


def _archived_dependency_ids(root) -> set[str]:
    index_path = archive_root(root) / "INDEX.csv"
    if not index_path.exists():
        return set()
    with index_path.open(newline="", encoding="utf-8") as handle:
        return {
            row["id"]
            for row in csv.DictReader(handle)
            if row.get("id")
        }


def _show_dependency_label(root, task) -> str:
    if not task.depends_on:
        return "-"

    active_statuses = {record.id: record.status for record in list_tasks(root, include_runtime=False)}
    archived_ids = _archived_dependency_ids(root)
    labels: list[str] = []
    for dependency_id in task.depends_on:
        if dependency_id in active_statuses:
            labels.append(f"{dependency_id} ({active_statuses[dependency_id]})")
        elif dependency_id in archived_ids:
            labels.append(f"{dependency_id} (archived)")
        else:
            labels.append(f"{dependency_id} (missing)")
    return ", ".join(labels)


def _cmd_status(args):
    root = args.workspace.resolve()
    config = load_config(args.workspace)
    state = load_state(args.workspace)
    monitoring = load_engine_monitoring(args.workspace)
    full_mode = bool(getattr(args, "full", False))

    if full_mode:
        return _cmd_status_full(args, root, config, state, monitoring)

    # --- Dashboard mode (default) ---
    active_task = (
        require_task(args.workspace, state.active_task_id) if state.active_task_id else None
    )

    # Active Task section
    for line in render_active_task_section(active_task, config.default_engine):
        print(line)

    # Last Completed section
    all_tasks = list_tasks_state_first(args.workspace, state=state)
    last_done = find_last_completed_task(all_tasks)
    print()
    for line in render_last_completed_section(last_done):
        print(line)

    # Queue section
    print()
    for line in render_queue_section(state.queue, all_tasks):
        print(line)

    # Engine Health section
    print()
    for line in render_engine_health_section(monitoring):
        print(line)
    for line_text in render_engine_monitoring_lines(monitoring):
        print(line_text)

    # Recent Activity section
    print()
    events = collect_recent_activity(root)
    for line in render_recent_activity_section(events):
        print(line)

    return 0


def _cmd_status_full(args, root, config, state, monitoring):
    """Full verbose status output (--full flag)."""
    print(f"workspace: {args.workspace}")
    print("status_read_mode: full")
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
    tasks = list_tasks(args.workspace)
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
        flag_reason = (
            f" flag_reason={_display_flag_reason(task)}" if task.status == "flagged" else ""
        )
        print(f"{task.id} [{task.status}/{task.pipeline_status}] {task.title}{flag_reason}")
    return 0


def _cmd_show(args):
    ensure_workspace(args.workspace)
    try:
        task = require_task(args.workspace, args.task_id)
    except ValueError:
        print(f"task not found: {args.task_id}")
        return 1
    print(f"id: {task.id}")
    print(f"slug: {task.slug}")
    print(f"title: {task.title}")
    print(f"status: {task.status}")
    print(f"flag_reason: {_display_flag_reason(task)}")
    print(f"pipeline_status: {task.pipeline_status}")
    print(f"priority: {task.priority}")
    print(f"engine: {task.engine or '-'}")
    print(f"model: {task.model or '-'}")
    print(f"task_type: {task.task_type or '-'}")
    print(f"mode: {task.mode}")
    print(f"pipeline_mode: {task.pipeline_mode}")
    print(f"depends_on: {_show_dependency_label(args.workspace, task)}")
    print(f"pm_complexity: {task.pm_complexity or '-'}")
    print(f"planned_effort: {task.planned_effort or '-'}")
    print(f"created_at: {task.created_at}")
    print(f"updated_at: {task.updated_at}")
    print(f"goal: {task.goal or '-'}")
    if task.acceptance_criteria:
        print("acceptance_criteria:")
        for criterion in task.acceptance_criteria:
            print(f"  - {criterion}")
    else:
        print("acceptance_criteria: -")
    if task.constraints:
        print("constraints:")
        for constraint in task.constraints:
            print(f"  - {constraint}")
    else:
        print("constraints: -")
    if task.plan:
        print("plan:")
        for step in task.plan:
            print(f"  - {step}")
    else:
        print("plan: -")
    if task.human_checkpoints:
        print(f"human_checkpoints: {', '.join(task.human_checkpoints)}")
    else:
        print("human_checkpoints: -")
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
