from pathlib import Path
from typing import Annotated
import csv

import typer

from litehive.cli.common import WorkspaceOption, choice, make_typer
from litehive.cli.display import task_dependencies_label, task_model_label
from litehive.cli.parse import (
    TASK_TYPE_CHOICES,
    parse_acceptance_criteria,
    parse_dependency_ids,
    parse_text_list_option,
)
from litehive.config.loading import load_config
from litehive.config.workspace import ensure_workspace
from litehive.tasks.archive import archive_root
from litehive.state.records import create_task, list_tasks as load_tasks, require_task
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.tasks.normalization import missing_acceptance_criteria_cli_warning
from litehive.tasks.constants import VALID_TASK_PRIORITIES
from litehive.tasks.status import abandon_task, close_task, update_task_metadata

app = make_typer(invoke_without_command=True)


def _display_flag_reason(task) -> str:
    if task.status != "flagged":
        return "-"
    return task.flag_reason or "unknown"


def _show_dependency_label(root, task) -> str:
    if not task.depends_on:
        return "-"

    active_statuses = {record.id: record.status for record in load_tasks(root, include_runtime=True, strict=False)}
    index_path = archive_root(root) / "INDEX.csv"
    archived_ids: set[str] = set()
    if index_path.exists():
        with index_path.open(newline="", encoding="utf-8") as handle:
            archived_ids = {row["id"] for row in csv.DictReader(handle) if row.get("id")}
    labels: list[str] = []
    for dependency_id in task.depends_on:
        if dependency_id in active_statuses:
            labels.append(f"{dependency_id} ({active_statuses[dependency_id]})")
        elif dependency_id in archived_ids:
            labels.append(f"{dependency_id} (archived)")
        else:
            labels.append(f"{dependency_id} (missing)")
    return ", ".join(labels)


@app.command("add", help="Create a queued task")
def add(
    title: Annotated[str, typer.Argument(help="Task title")],
    workspace: WorkspaceOption = Path.cwd(),
    goal: Annotated[str, typer.Option(help="Task goal text")] = "",
    acceptance_criteria: Annotated[
        list[str] | None, typer.Option(help="Add one acceptance criterion; repeat for multiple")
    ] = None,
    depends_on: Annotated[
        list[str] | None, typer.Option(help="Add prerequisite task ids; repeat or use comma-separated values")
    ] = None,
    task_type: Annotated[
        str | None, typer.Option(click_type=choice(TASK_TYPE_CHOICES), help="Explicit routing class for this task")
    ] = None,
    pipeline_mode: Annotated[
        str,
        typer.Option(
            "--mode",
            "--pipeline-mode",
            click_type=choice(["full", "single"]),
            help="Task pipeline mode",
        ),
    ] = "full",
    priority: Annotated[
        str | None, typer.Option(click_type=choice(VALID_TASK_PRIORITIES), help="Task priority")
    ] = None,
) -> int:
    ensure_workspace(workspace)
    try:
        depends_on = parse_dependency_ids(depends_on)
        acceptance_criteria = parse_acceptance_criteria(acceptance_criteria)
        task = create_task(
            workspace,
            title=title,
            depends_on=None if depends_on is ... else depends_on,
            pipeline_mode=pipeline_mode,
            goal=goal,
            acceptance_criteria=None if acceptance_criteria is ... else acceptance_criteria,
            task_type=task_type,
            priority=priority,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"add failed: {exc}")
        return 1
    print(f"Created task {task.id} in {workspace / '.litehive' / 'tasks' / (task.id + '-' + task.slug)}")
    print(f"priority: {task.priority}")
    print(f"retry_limit: {task.retry_policy.max_retries if task.retry_policy.max_retries is not None else 'default'}")
    print(f"pipeline_mode: {task.pipeline_mode}")
    print(f"model: {task_model_label(task.model)}")
    print(f"task_type: {task.task_type or '-'}")
    print(f"depends_on: {task_dependencies_label(task.id, task.depends_on)}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


@app.command("debug", help="Inspect subagent artifacts for a task")
def debug(
    task_id: Annotated[str, typer.Argument(help="Task ID (e.g. T-0001)")],
    workspace: WorkspaceOption = Path.cwd(),
    all_: Annotated[bool, typer.Option("--all", help="List all subagents")] = False,
    worktree: Annotated[bool, typer.Option(help="Show worktree details")] = False,
) -> int:
    from litehive.cli.task_debug_support import _debug_all, _debug_latest, _debug_worktree

    ensure_workspace(workspace)
    try:
        task = require_task(workspace, task_id)
    except ValueError:
        print(f"task not found: {task_id}")
        return 1
    if worktree:
        return _debug_worktree(workspace, task)
    if all_:
        return _debug_all(workspace, task)
    return _debug_latest(workspace, task)


@app.command("logs", help="Show daemon, task journal, and subagent logs")
def logs(
    task_id: Annotated[str | None, typer.Argument(help="Optional task ID")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    daemon: Annotated[bool, typer.Option(help="List latest daemon sessions")] = False,
    agent: Annotated[bool, typer.Option(help="Show subagent transcript/stdout")] = False,
    all_: Annotated[bool, typer.Option("--all", help="List all subagent runs")] = False,
    follow: Annotated[bool, typer.Option(help="Follow live stdout")] = False,
) -> int:
    from litehive.cli.task_logs_support import (
        _follow_active_subagent,
        _list_daemon_sessions,
        _list_task_subagents,
        _load_task_with_runtime,
        _show_latest_daemon_log,
        _show_latest_subagent,
        _show_task_journal,
    )

    ensure_workspace(workspace)
    if follow:
        return _follow_active_subagent(workspace, task_id=task_id)
    if daemon:
        return _list_daemon_sessions(workspace)
    if task_id:
        task = _load_task_with_runtime(workspace, task_id)
        if task is None:
            print(f"task not found: {task_id}")
            return 1
        if agent:
            if all_:
                return _list_task_subagents(workspace, task)
            return _show_latest_subagent(workspace, task)
        return _show_task_journal(workspace, task)
    return _show_latest_daemon_log(workspace)


@app.command("list", help="Compact task listing with optional filters")
def list_tasks(
    workspace: WorkspaceOption = Path.cwd(),
    show_all: Annotated[bool, typer.Option("--all", help="Include done tasks")] = False,
    filter_status: Annotated[str | None, typer.Option("--status", help="Filter by task status")] = None,
    filter_pipeline_status: Annotated[
        str | None, typer.Option("--pipeline-status", help="Filter by pipeline stage")
    ] = None,
    filter_engine: Annotated[str | None, typer.Option("--engine", help="Filter by engine")] = None,
) -> int:
    ensure_workspace(workspace)
    config = load_config(workspace)
    tasks = load_tasks(workspace)
    filtered = []
    for task in tasks:
        if not show_all and task.status == "done":
            continue
        if filter_status and task.status != filter_status:
            continue
        if filter_pipeline_status and task.pipeline_status != filter_pipeline_status:
            continue
        if filter_engine and config.default_engine != filter_engine:
            continue
        filtered.append(task)
    for task in filtered:
        flag_reason = f" flag_reason={_display_flag_reason(task)}" if task.status == "flagged" else ""
        print(f"{task.id} [{task.status}/{task.pipeline_status}] {task.title}{flag_reason}")
    return 0


@app.command("show", help="Print full details for a single task")
def show(task_id: Annotated[str, typer.Argument(help="Task ID")], workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    try:
        task = require_task(workspace, task_id)
    except ValueError:
        print(f"task not found: {task_id}")
        return 1
    print(f"id: {task.id}")
    print(f"slug: {task.slug}")
    print(f"title: {task.title}")
    print(f"status: {task.status}")
    print(f"flag_reason: {_display_flag_reason(task)}")
    print(f"pipeline_stage: {task.pipeline_status}")
    print(f"priority: {task.priority}")
    print(f"engine: {load_config(workspace).default_engine}")
    print(f"model: {task.model or '-'}")
    print(f"task_type: {task.task_type or '-'}")
    print(f"pipeline_mode: {task.pipeline_mode}")
    print(f"depends_on: {_show_dependency_label(workspace, task)}")
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
        for item in task.constraints:
            print(f"  - {item}")
    else:
        print("constraints: -")
    if task.plan:
        print("plan:")
        for step in task.plan:
            print(f"  - {step}")
    else:
        print("plan: -")
    rt = task.runtime
    print(f"execution_status: {rt.execution_status or '-'}")
    print(f"current_stage: {rt.current_stage.stage if rt.current_stage and rt.current_stage.stage else '-'}")
    print(f"retry_count: {rt.retry_count}")
    print(f"last_outcome: {rt.last_outcome or '-'}")
    return 0


@app.command("abandon", help="Cancel a flagged or closed task and remove it from the queue")
def abandon(task_id: Annotated[str, typer.Argument(help="Task id")], workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    try:
        task = abandon_task(workspace, task_id)
    except ValueError as exc:
        print(f"abandon failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print("status: cancelled")
    print(f"pipeline_stage: {task.pipeline_status}")
    return 0


@app.command("close", help="Close a task with an explicit non-implementation outcome")
def close(
    task_id: Annotated[str, typer.Argument(help="Task id")],
    workspace: WorkspaceOption = Path.cwd(),
    outcome: Annotated[
        str, typer.Option(click_type=choice(["wont_do", "deferred", "duplicate"]), help="Close reason")
    ] = ...,
    reason: Annotated[str | None, typer.Option(help="Optional rationale")] = None,
    follow_up_task: Annotated[str | None, typer.Option(help="Optional follow-up task id")] = None,
) -> int:
    from litehive.cli.agent_cli import block_if_agent

    block_if_agent()
    ensure_workspace(workspace)
    try:
        task = close_task(
            workspace,
            task_id,
            outcome=outcome,
            reason=reason,
            follow_up_task_id=follow_up_task,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"close failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print(f"status: {task.status}")
    print(f"outcome: {task.runtime.last_outcome.reason_code}")
    print(f"reason: {task.runtime.last_outcome.reason}")
    print(f"follow_up_task: {task.runtime.last_outcome.follow_up_task_id or '-'}")
    print(f"pipeline_stage: {task.pipeline_status}")
    return 0


@app.command("update", help="Update task metadata")
def update(
    task_id: Annotated[str, typer.Argument(help="Task id")],
    workspace: WorkspaceOption = Path.cwd(),
    title: Annotated[str | None, typer.Option(help="Replace the task title")] = None,
    priority: Annotated[
        str | None, typer.Option(click_type=choice(VALID_TASK_PRIORITIES), help="Set task priority")
    ] = None,
    goal: Annotated[str | None, typer.Option(help="Replace the task goal")] = None,
    depends_on: Annotated[list[str] | None, typer.Option(help="Replace task dependencies")] = None,
    acceptance_criteria: Annotated[list[str] | None, typer.Option(help="Replace acceptance criteria")] = None,
    constraint: Annotated[list[str] | None, typer.Option(help="Replace constraints")] = None,
    plan_step: Annotated[list[str] | None, typer.Option(help="Replace the plan")] = None,
) -> int:
    from litehive.cli.agent_cli import block_if_agent

    block_if_agent()
    ensure_workspace(workspace)
    if (
        title is None
        and depends_on is None
        and acceptance_criteria is None
        and constraint is None
        and plan_step is None
        and priority is None
        and goal is None
    ):
        print("update failed: no changes requested")
        return 1
    try:
        depends_on = parse_dependency_ids(depends_on, task_id=task_id, allow_clear=True)
        acceptance_criteria = parse_acceptance_criteria(acceptance_criteria, allow_clear=True)
        constraints = parse_text_list_option(constraint, option_name="Constraints", allow_clear=True)
        plan = parse_text_list_option(plan_step, option_name="Plan steps", allow_clear=True)

        task = update_task_metadata(
            workspace,
            task_id,
            title=title if title is not None else ...,
            depends_on=depends_on,
            priority=priority if priority is not None else ...,
            goal=goal if goal is not None else ...,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            plan=plan,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"update failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print(f"model: {task_model_label(task.model)}")
    print(f"retry_limit: {task.retry_policy.max_retries if task.retry_policy.max_retries is not None else 'default'}")
    print(f"priority: {task.priority}")
    print(f"auto_commit: {task.git.auto_commit}")
    print(f"task_type: {task.task_type or '-'}")
    print(f"depends_on: {task_dependencies_label(task.id, task.depends_on)}")
    print(f"goal: {task.goal}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    print(f"constraints: {len(task.constraints)}")
    print(f"plan: {len(task.plan)}")
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0
