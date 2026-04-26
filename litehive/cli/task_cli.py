from pathlib import Path
from typing import Annotated

import typer

from litehive.cli.common import WorkspaceOption, choice, make_typer
from litehive.cli.display import task_dependencies_label, task_model_label
from litehive.cli.parse import (
    parse_acceptance_criteria,
    parse_dependency_ids,
    parse_text_list_option,
)
from litehive.config.loading import load_config
from litehive.config.workspace import ensure_workspace
from litehive.tasks.archive import archive_tasks, get_archived_task, list_archived_tasks
from litehive.tasks.browse import list_recently_created_tasks
from litehive.tasks.recent import (
    format_elapsed_duration,
    list_recent_task_summaries,
)
from litehive.state.records import create_task, get_task, list_tasks as load_tasks, require_task
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.tasks.normalization import missing_acceptance_criteria_cli_warning
from litehive.tasks.constants import VALID_TASK_PRIORITIES
from litehive.tasks.duplicates import (
    DuplicateTaskMatch,
    TaskSearchMatch,
    find_potential_duplicate_tasks,
    search_tasks_by_text,
)
from litehive.tasks.status import abandon_task, close_task, update_task_metadata

app = make_typer(invoke_without_command=True)


def _display_flag_reason(task) -> str:
    if task.status != "flagged":
        return "-"
    return task.flag_reason or "unknown"


def _display_close_reason(task) -> str:
    if task.status not in {"closed", "done", "archived"}:
        return "-"
    return task.close_reason or task.runtime.pipeline.last_outcome.reason_code or "unknown"


def _show_dependency_label(root, task) -> str:
    if not task.depends_on:
        return "-"

    active_statuses = {
        record.id: record.status
        for record in load_tasks(root, include_runtime=True, strict=False, include_archived=True)
    }
    labels: list[str] = []
    for dependency_id in task.depends_on:
        if dependency_id in active_statuses:
            labels.append(f"{dependency_id} ({active_statuses[dependency_id]})")
        else:
            labels.append(f"{dependency_id} (missing)")
    return ", ".join(labels)


def _load_task_with_archive_history(root: Path, task_id: str):
    task = get_task(root, task_id)
    if task is not None:
        return task
    return get_archived_task(root, task_id)


def _load_task_list_with_archive_history(root: Path, *, include_archived: bool) -> list:
    tasks = load_tasks(root, strict=False)
    if not include_archived:
        return tasks
    seen = {task.id for task in tasks}
    tasks.extend(task for task in list_archived_tasks(root) if task.id not in seen)
    return tasks


def _terminal_status_label(task) -> str:
    if task.status == "archived":
        reason = task.close_reason or task.runtime.pipeline.last_outcome.reason_code
        if reason:
            if str(reason) == "execution_cancelled":
                return "cancelled"
            return str(reason)
        if str(task.pipeline_status) == "done":
            return "done"
        return "archived"
    if task.status != "closed":
        return str(task.status)
    close_reason = task.close_reason or task.runtime.pipeline.last_outcome.reason_code or "closed"
    if str(close_reason) == "execution_cancelled":
        return "cancelled"
    return str(close_reason)


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


def _print_duplicate_task_warning(matches: list[DuplicateTaskMatch]) -> None:
    if not matches:
        return
    print("warning: potential duplicate tasks found:")
    for match in matches:
        print(f"  - {match.task_id} [{match.status}] {match.title}")


def _print_task_search_results(matches: list[TaskSearchMatch]) -> None:
    for index, match in enumerate(matches):
        if index:
            print()
        print(f"{match.task_id} [{match.status}] {match.title}")
        print(f"  goal: {match.snippet or '-'}")


def _print_recent_task_table(summaries) -> None:
    columns = [
        ("task_id", "TASK_ID"),
        ("title", "TITLE"),
        ("transition_count", "TRANSITIONS"),
        ("elapsed", "ELAPSED"),
        ("final_stage", "FINAL_STAGE"),
        ("status", "STATUS"),
    ]
    rows = [
        {
            "task_id": summary.task_id,
            "title": summary.title,
            "transition_count": str(summary.transition_count),
            "elapsed": format_elapsed_duration(summary.elapsed_seconds),
            "final_stage": summary.final_stage,
            "status": summary.status,
        }
        for summary in summaries
    ]
    widths = {key: max(len(header), *(len(row[key]) for row in rows)) for key, header in columns}
    print("  ".join(header.ljust(widths[key]) for key, header in columns))
    for row in rows:
        print("  ".join(row[key].ljust(widths[key]) for key, _ in columns))


def _print_created_task_table(rows) -> None:
    columns = [
        ("task_id", "TASK_ID"),
        ("title", "TITLE"),
        ("created_at", "CREATED_AT"),
        ("source", "SOURCE"),
        ("context", "CONTEXT"),
    ]
    rendered = [
        {
            "task_id": row.task_id,
            "title": row.title,
            "created_at": row.created_at,
            "source": row.source,
            "context": row.context,
        }
        for row in rows
    ]
    widths = {key: max(len(header), *(len(row[key]) for row in rendered)) for key, header in columns}
    print("  ".join(header.ljust(widths[key]) for key, header in columns))
    for row in rendered:
        print("  ".join(row[key].ljust(widths[key]) for key, _ in columns))


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
        _print_duplicate_task_warning(find_potential_duplicate_tasks(workspace, title=title, goal=goal))
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
    print(f"task_type: {task.task_type or '-'}")
    print(f"depends_on: {task_dependencies_label(task.id, task.depends_on)}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


@app.command("search", help="Search tasks using the maintained sqlitesearch index")
def search(
    query: Annotated[str, typer.Argument(help="Free-text search query")],
    workspace: WorkspaceOption = Path.cwd(),
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum matches to show")] = 5,
) -> int:
    ensure_workspace(workspace)
    matches = search_tasks_by_text(workspace, query=query, limit=limit)
    if not matches:
        print(f"No matching tasks found for: {query}")
        return 0
    _print_task_search_results(matches)
    return 0


@app.command("debug", help="Inspect subagent artifacts for a task")
def debug(
    task_id: Annotated[str, typer.Argument(help="Task ID (e.g. T-0001)")],
    workspace: WorkspaceOption = Path.cwd(),
    all_: Annotated[bool, typer.Option("--all", help="List all subagents")] = False,
    worktree: Annotated[bool, typer.Option(help="Show worktree details")] = False,
) -> int:
    from litehive.cli.task_debug_support import debug_all, debug_latest, debug_worktree

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


@app.command("logs", help="Show daemon, task journal, and subagent logs")
def logs(
    task_id: Annotated[str | None, typer.Argument(help="Optional task ID")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    daemon: Annotated[bool, typer.Option(help="List latest daemon sessions")] = False,
    agent: Annotated[bool, typer.Option(help="Show subagent execution trace/stdout")] = False,
    all_: Annotated[bool, typer.Option("--all", help="List all subagent runs")] = False,
    follow: Annotated[bool, typer.Option(help="Follow live stdout")] = False,
) -> int:
    from litehive.cli.task_logs_support import (
        follow_active_subagent,
        list_daemon_sessions,
        list_task_subagents,
        load_task_with_runtime,
        show_latest_daemon_log,
        show_latest_subagent,
        show_task_journal,
    )

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


@app.command("list", help="Compact task listing with optional filters")
def list_tasks(
    workspace: WorkspaceOption = Path.cwd(),
    show_all: Annotated[bool, typer.Option("--all", help="Include done and archived tasks")] = False,
    filter_status: Annotated[str | None, typer.Option("--status", help="Filter by task status")] = None,
    filter_pipeline_status: Annotated[
        str | None, typer.Option("--pipeline-status", help="Filter by pipeline stage")
    ] = None,
    filter_engine: Annotated[str | None, typer.Option("--engine", help="Filter by engine")] = None,
) -> int:
    ensure_workspace(workspace)
    config = load_config(workspace)
    tasks = _load_task_list_with_archive_history(
        workspace,
        include_archived=show_all or filter_status == "archived",
    )
    filtered = []
    for task in tasks:
        archived = task.status == "archived"
        if not show_all and filter_status != "archived" and task.status == "done":
            continue
        if filter_status == "archived":
            if not archived:
                continue
        elif filter_status and task.status != filter_status:
            continue
        if filter_pipeline_status and task.pipeline_status != filter_pipeline_status:
            continue
        if filter_engine and config.default_engine != filter_engine:
            continue
        filtered.append(task)
    for task in filtered:
        flag_reason = f" flag_reason={_display_flag_reason(task)}" if task.status == "flagged" else ""
        close_reason = (
            f" close_reason={_display_close_reason(task)}" if task.status in {"closed", "done", "archived"} else ""
        )
        print(f"{task.id} [{task.status}/{task.pipeline_status}] {task.title}{flag_reason}{close_reason}")
    return 0


@app.command("archive", help="Archive explicit done or closed terminal task IDs")
def archive(
    task_ids: Annotated[list[str], typer.Argument(help="Task IDs to archive")],
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    ensure_workspace(workspace)
    try:
        tasks = archive_tasks(workspace, task_ids)
    except ValueError as exc:
        print(f"archive failed: {exc}")
        return 1
    for task in tasks:
        print(f"archived: {task.id} {task.title}")
        print(f"status: {_terminal_status_label(task)}")
    print(f"archived_count: {len(tasks)}")
    return 0


@app.command("show", help="Print full details for a single task")
def show(task_id: Annotated[str, typer.Argument(help="Task ID")], workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    task = _load_task_with_archive_history(workspace, task_id)
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
    print(f"task_type: {task.task_type or '-'}")
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


@app.command("recent", help="Summarize tasks touched in a recent time window")
def recent(
    workspace: WorkspaceOption = Path.cwd(),
    since: Annotated[
        str,
        typer.Option("--since", help="Compact duration window (e.g. 24h, 90m, 7d)"),
    ] = "24h",
) -> int:
    ensure_workspace(workspace)
    try:
        summaries = list_recent_task_summaries(workspace, since=since)
    except ValueError as exc:
        print(f"recent failed: {exc}")
        return 1
    if not summaries:
        print(f"No tasks touched in the last {since}.")
        return 0
    _print_recent_task_table(summaries)
    return 0


@app.command("browse", help="List tasks created in a recent time window with provenance context")
def browse(
    workspace: WorkspaceOption = Path.cwd(),
    since: Annotated[
        str,
        typer.Option("--since", help="Compact duration window (e.g. 24h, 90m, 7d)"),
    ] = "24h",
) -> int:
    ensure_workspace(workspace)
    try:
        rows = list_recently_created_tasks(workspace, since=since)
    except ValueError as exc:
        print(f"browse failed: {exc}")
        return 1
    if not rows:
        print(f"No tasks created in the last {since}.")
        return 0
    _print_created_task_table(rows)
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


@app.command("close", help="Close a task with an explicit terminal outcome")
def close(
    task_id: Annotated[str, typer.Argument(help="Task id")],
    workspace: WorkspaceOption = Path.cwd(),
    outcome: Annotated[
        str, typer.Option(click_type=choice(["done", "wont_do", "deferred", "duplicate"]), help="Close reason")
    ] = ...,
    reason: Annotated[str | None, typer.Option(help="Optional rationale")] = None,
    follow_up_task: Annotated[str | None, typer.Option(help="Optional follow-up task id")] = None,
) -> int:
    from litehive.cli.agent_cli import current_agent_role, require_agent_role

    agent_role = current_agent_role()
    close_kwargs: dict[str, str] = {}
    if agent_role is not None:
        require_agent_role({"planner", "reviewer"})
        close_kwargs = {"audit_actor": "agent", "audit_source": "agent"}
    ensure_workspace(workspace)
    try:
        task = close_task(
            workspace,
            task_id,
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
    from litehive.cli.agent_cli import current_agent_role, require_agent_role

    agent_role = current_agent_role()
    update_kwargs: dict[str, object] = {}
    if agent_role is not None:
        require_agent_role({"planner", "reviewer"})
        update_kwargs = {
            "allow_active_agent_task_mutation": True,
            "audit_actor": "agent",
            "audit_source": "agent",
        }
    ensure_workspace(workspace)
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
        depends_on = parse_dependency_ids(depends_on, task_id=task_id, allow_clear=True)
        acceptance_criteria = parse_acceptance_criteria(acceptance_criteria, allow_clear=True)
        constraints = parse_text_list_option(constraints, option_name="constraints", allow_clear=True)
        plan = parse_text_list_option(plan, option_name="plan", allow_clear=True)

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
