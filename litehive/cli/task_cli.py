from pathlib import Path
from typing import Annotated

import typer

from litehive.cli.agent_cli import current_agent_role, resolve_active_agent_task_mutation_target
from litehive.cli.common import WorkspaceOption, choice, make_typer
from litehive.cli.display import task_dependencies_label, task_model_label
from litehive.cli.parse import (
    parse_acceptance_criteria,
    parse_dependency_ids,
    parse_text_list_option,
)
from litehive.cli.task_debug_support import (
    debug_all,
    debug_latest,
    debug_worktree,
    render_task_evidence,
)
from litehive.cli.task_logs_support import (
    follow_active_subagent,
    list_daemon_sessions,
    list_task_subagents,
    load_task_with_runtime,
    show_latest_daemon_log,
    show_latest_subagent,
    show_task_journal,
)
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task, get_task, list_tasks as load_tasks, require_task
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.tasks.normalization import missing_acceptance_criteria_cli_warning
from litehive.tasks.constants import VALID_TASK_PRIORITIES
from litehive.tasks.status import abandon_task, close_task, update_task

app = make_typer(invoke_without_command=True)


def _display_flag_reason(task) -> str:
    if task.status != "flagged":
        return "-"
    return task.flag_reason or "unknown"


def _display_close_reason(task) -> str:
    if task.status not in {"closed", "done"}:
        return "-"
    return task.close_reason or task.runtime.pipeline.last_outcome.reason_code or "unknown"


def _show_dependency_label(root, task) -> str:
    if not task.depends_on:
        return "-"

    active_statuses = {record.id: record.status for record in load_tasks(root, include_runtime=True, strict=False)}
    labels: list[str] = []
    for dependency_id in task.depends_on:
        if dependency_id in active_statuses:
            labels.append(f"{dependency_id} ({active_statuses[dependency_id]})")
        else:
            labels.append(f"{dependency_id} (missing)")
    return ", ".join(labels)


def _print_creation_provenance(task) -> None:
    created_from = task.created_from
    if created_from is None:
        print("created_from: -")
        return
    print("created_from:")
    print(f"  source: {created_from.source}")
    print(f"  task_id: {created_from.task_id or '-'}")
    print(f"  stage: {created_from.stage or '-'}")
    print(f"  role: {created_from.role or '-'}")
    print(f"  blocking: {'yes' if created_from.blocking else 'no'}")
    print(f"  rationale: {created_from.rationale or '-'}")


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
            goal=goal,
            acceptance_criteria=None if acceptance_criteria is ... else acceptance_criteria,
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
    print(f"depends_on: {task_dependencies_label(task.id, task.depends_on)}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


@app.command("evidence", help="Show minimal routing and recovery evidence for a task")
def evidence(
    task_id: Annotated[str, typer.Argument(help="Task ID (e.g. T-0001)")],
    workspace: WorkspaceOption = Path.cwd(),
) -> int:

    ensure_workspace(workspace)
    try:
        task = require_task(workspace, task_id)
    except ValueError:
        print(f"task not found: {task_id}")
        return 1
    return render_task_evidence(workspace, task)


@app.command("debug", help="Compatibility alias for `task evidence`")
def debug(
    task_id: Annotated[str, typer.Argument(help="Task ID (e.g. T-0001)")],
    workspace: WorkspaceOption = Path.cwd(),
    all_: Annotated[bool, typer.Option("--all", help="List all subagents")] = False,
    worktree: Annotated[bool, typer.Option(help="Show worktree details")] = False,
) -> int:

    ensure_workspace(workspace)
    try:
        task = require_task(workspace, task_id)
    except ValueError:
        print(f"task not found: {task_id}")
        return 1
    if worktree:
        return debug_worktree(workspace, task)
    if all_:
        return debug_all(workspace, task)
    return debug_latest(workspace, task)


@app.command("logs", help="Show daemon logs, task journal, and compact subagent evidence")
def logs(
    task_id: Annotated[str | None, typer.Argument(help="Optional task ID")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    daemon: Annotated[bool, typer.Option(help="List latest daemon sessions")] = False,
    agent: Annotated[bool, typer.Option(help="Show compact DB-backed subagent evidence")] = False,
    all_: Annotated[bool, typer.Option("--all", help="List all subagent runs")] = False,
    follow: Annotated[bool, typer.Option(help="Follow live stdout")] = False,
) -> int:

    ensure_workspace(workspace)
    if follow:
        return follow_active_subagent(workspace, task_id=task_id)
    if daemon:
        return list_daemon_sessions(workspace)
    if task_id:
        task = load_task_with_runtime(workspace, task_id)
        if task is None:
            print(f"task not found: {task_id}")
            return 1
        if agent:
            if all_:
                return list_task_subagents(workspace, task)
            return show_latest_subagent(workspace, task)
        return show_task_journal(workspace, task)
    return show_latest_daemon_log(workspace)


@app.command("list", help="Compact task listing")
def list_tasks(
    workspace: WorkspaceOption = Path.cwd(),
    show_all: Annotated[bool, typer.Option("--all", help="Include done tasks")] = False,
) -> int:
    ensure_workspace(workspace)
    tasks = load_tasks(workspace, strict=False)
    filtered = []
    for task in tasks:
        if not show_all and task.status == "done":
            continue
        filtered.append(task)
    for task in filtered:
        flag_reason = f" flag_reason={_display_flag_reason(task)}" if task.status == "flagged" else ""
        close_reason = f" close_reason={_display_close_reason(task)}" if task.status in {"closed", "done"} else ""
        print(f"{task.id} [{task.status}/{task.pipeline_status}] {task.title}{flag_reason}{close_reason}")
    return 0


@app.command("show", help="Print full details for a single task")
def show(task_id: Annotated[str, typer.Argument(help="Task ID")], workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    task = get_task(workspace, task_id)
    if task is None:
        print(f"task not found: {task_id}")
        return 1
    print(f"id: {task.id}")
    print(f"slug: {task.slug}")
    print(f"title: {task.title}")
    print(f"status: {task.status}")
    print(f"close_reason: {_display_close_reason(task)}")
    print(f"flag_reason: {_display_flag_reason(task)}")
    print(f"pipeline_stage: {task.pipeline_status}")
    print(f"priority: {task.priority}")
    print(f"model: {task.model or '-'}")
    print(f"pipeline_mode: {task.pipeline_mode}")
    print(f"depends_on: {_show_dependency_label(workspace, task)}")
    print(f"created_at: {task.created_at}")
    print(f"updated_at: {task.updated_at}")
    _print_creation_provenance(task)
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
    pipeline_runtime = task.runtime.pipeline
    print(f"execution_status: {pipeline_runtime.execution_status or '-'}")
    current_stage = pipeline_runtime.current_stage
    print(f"current_stage: {current_stage.stage if current_stage and current_stage.stage else '-'}")
    print(f"retry_count: {pipeline_runtime.retry_count}")
    print(f"last_outcome: {pipeline_runtime.last_outcome or '-'}")
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
    print(f"status: {task.status}")
    print(f"close_reason: {_display_close_reason(task)}")
    print(f"pipeline_stage: {task.pipeline_status}")
    return 0


@app.command("close", help="Close a task with an explicit close reason")
def close(
    task_id: Annotated[str, typer.Argument(help="Task id")],
    workspace: WorkspaceOption = Path.cwd(),
    outcome: Annotated[
        str, typer.Option(click_type=choice(["done", "wont_do", "deferred", "duplicate"]), help="Close reason")
    ] = ...,
    reason: Annotated[str | None, typer.Option(help="Optional rationale")] = None,
    follow_up_task: Annotated[str | None, typer.Option(help="Optional follow-up task id")] = None,
) -> int:

    agent_role = current_agent_role()
    close_kwargs: dict[str, str] = {}
    mutation_workspace = workspace
    mutation_task_id = task_id
    if agent_role is not None:
        target = resolve_active_agent_task_mutation_target(task_id, allowed_roles={"planner", "reviewer"})
        mutation_workspace = target.root
        mutation_task_id = target.task_id
        close_kwargs = {"audit_actor": "agent", "audit_source": "agent"}
    ensure_workspace(mutation_workspace)
    try:
        task = close_task(
            mutation_workspace,
            mutation_task_id,
            outcome=outcome,
            reason=reason,
            follow_up_task_id=follow_up_task,
            **close_kwargs,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"close failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print(f"status: {task.status}")
    print(f"close_reason: {_display_close_reason(task)}")
    print(f"reason: {task.runtime.pipeline.last_outcome.reason}")
    print(f"follow_up_task: {task.runtime.pipeline.last_outcome.follow_up_task_id or '-'}")
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
    constraints: Annotated[list[str] | None, typer.Option("--constraint", help="Replace task constraints")] = None,
    plan: Annotated[list[str] | None, typer.Option("--plan-step", help="Replace task plan steps")] = None,
) -> int:

    agent_role = current_agent_role()
    update_kwargs: dict[str, object] = {}
    mutation_workspace = workspace
    mutation_task_id = task_id
    if agent_role is not None:
        target = resolve_active_agent_task_mutation_target(task_id, allowed_roles={"planner", "reviewer"})
        mutation_workspace = target.root
        mutation_task_id = target.task_id
        update_kwargs = {
            "allow_active_agent_task_mutation": True,
            "audit_actor": "agent",
            "audit_source": "agent",
        }
    ensure_workspace(mutation_workspace)
    if (
        title is None
        and depends_on is None
        and acceptance_criteria is None
        and priority is None
        and goal is None
        and constraints is None
        and plan is None
    ):
        print("update failed: no changes requested")
        return 1
    try:
        depends_on = parse_dependency_ids(depends_on, task_id=mutation_task_id, allow_clear=True)
        acceptance_criteria = parse_acceptance_criteria(acceptance_criteria, allow_clear=True)
        constraints = parse_text_list_option(constraints, option_name="constraints", allow_clear=True)
        plan = parse_text_list_option(plan, option_name="plan", allow_clear=True)

        task = update_task(
            mutation_workspace,
            mutation_task_id,
            title=title if title is not None else ...,
            depends_on=depends_on,
            priority=priority if priority is not None else ...,
            goal=goal if goal is not None else ...,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            plan=plan,
            **update_kwargs,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"update failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print(f"model: {task_model_label(task.model)}")
    print(f"retry_limit: {task.retry_policy.max_retries if task.retry_policy.max_retries is not None else 'default'}")
    print(f"priority: {task.priority}")
    print(f"auto_commit: {task.git.auto_commit}")
    print(f"depends_on: {task_dependencies_label(task.id, task.depends_on)}")
    print(f"goal: {task.goal}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    print(f"constraints: {len(task.constraints)}")
    print(f"plan: {len(task.plan)}")
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0
