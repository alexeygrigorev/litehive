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
from litehive.state.records import create_task, get_task, list_tasks, require_task
from litehive.domain.common import TaskStatus
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.tasks.normalization import missing_acceptance_criteria_cli_warning
from litehive.tasks.constants import VALID_TASK_PRIORITIES
from litehive.tasks.status import abandon_task, close_task, update_task

app = make_typer(invoke_without_command=True)


def _display_flag_reason(task) -> str:
    """
    Render the flag-reason cell for ``task list`` and ``task show``.

    Non-flagged rows collapse to ``-`` so the column reads cleanly
    rather than littering most rows with empty reasons. Flagged
    rows without a stored reason fall back to ``"unknown"`` so the
    cell is never silently blank.
    """
    if task.status != TaskStatus.FLAGGED:
        return "-"
    return task.flag_reason or "unknown"


def _display_close_reason(task) -> str:
    """
    Render the close-reason cell with a layered fallback.

    Order of preference: explicit ``close_reason``, then the
    pipeline's last outcome ``reason_code``, then ``"unknown"``.
    The fallback chain ensures closed tasks always carry *some*
    reason in operator output even when the close path forgot to
    set the explicit field — better than rendering a misleading
    blank cell.
    """
    if task.status not in {TaskStatus.CLOSED, TaskStatus.DONE}:
        return "-"
    return task.close_reason or task.runtime.pipeline.last_outcome.reason_code or "unknown"


def _show_dependency_label(root, task) -> str:
    """
    Render the ``depends_on`` cell with each dependency's current status inline.

    Inlines each prerequisite's status (or ``missing`` for a stale
    edge) so an operator can spot a broken dependency without an
    extra ``task show`` per id. Loaded with ``strict=False`` so a
    corrupted record does not block the rest of the listing.
    """
    if not task.depends_on:
        return "-"

    active_statuses = {record.id: record.status for record in list_tasks(root, include_runtime=True, strict=False)}
    labels: list[str] = []
    for dependency_id in task.depends_on:
        if dependency_id in active_statuses:
            labels.append(f"{dependency_id} ({active_statuses[dependency_id]})")
        else:
            labels.append(f"{dependency_id} (missing)")
    return ", ".join(labels)


def _print_creation_provenance(task) -> None:
    """
    Print the ``created_from`` block on ``task show``.

    Lets operators trace whether a task was created by a human, a
    planner subagent, or a follow-up edge from another stage —
    valuable when triaging unexpected work in the queue. ``-`` is
    printed when no provenance was recorded so the section keeps a
    fixed shape across tasks.
    """
    created_from = task.created_from
    if created_from is None:
        print("created_from: -")
        return
    print("created_from:")
    print(f"  source: {created_from.source}")
    print(f"  task_id: {created_from.task_id or '-'}")
    print(f"  stage: {created_from.stage or '-'}")
    print(f"  role: {created_from.role or '-'}")
    if created_from.blocking:
        blocking_label = "yes"
    else:
        blocking_label = "no"
    print(f"  blocking: {blocking_label}")
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
    """
    Queue a new task.

    Operator entry point for queueing new work. Warns (but does
    not fail) when acceptance criteria are missing so the planner
    is not handed an underspecified goal silently — the planner
    can still triage the task, but the operator sees the gap on
    creation rather than in a downstream rejection.
    """
    ensure_workspace(workspace)
    try:
        depends_on = parse_dependency_ids(depends_on)
        acceptance_criteria = parse_acceptance_criteria(acceptance_criteria)
        if depends_on is ...:
            depends_on_arg = None
        else:
            depends_on_arg = depends_on
        if acceptance_criteria is ...:
            acceptance_criteria_arg = None
        else:
            acceptance_criteria_arg = acceptance_criteria
        task = create_task(
            workspace,
            title=title,
            depends_on=depends_on_arg,
            goal=goal,
            acceptance_criteria=acceptance_criteria_arg,
            priority=priority,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"add failed: {exc}")
        return 1
    print(f"Created task {task.id} in {workspace / '.litehive' / 'tasks' / (task.id + '-' + task.slug)}")
    print(f"priority: {task.priority}")
    if task.retry_policy.max_retries is not None:
        retry_limit_label = task.retry_policy.max_retries
    else:
        retry_limit_label = "default"
    print(f"retry_limit: {retry_limit_label}")
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
    """
    Operator triage view of a stuck task.

    Surfaces only the routing/recovery signal an operator needs to
    decide what to do with a parked task (current stage, recovery
    triggers, latest verdicts). Distinct from the verbose
    ``task show`` because most triage decisions only need the
    pipeline-level evidence and not the task fields.
    """
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
    """
    Compatibility alias for ``task evidence`` and related views.

    Kept so muscle-memory ``task debug`` invocations still resolve
    after the rename to ``evidence``. ``--worktree`` routes to the
    worktree-detail view, ``--all`` to the every-subagent listing,
    otherwise the latest-subagent view; the operator picks the
    flavor without remembering three separate command names.
    """
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
    """
    Single operator-facing entry point for log-style evidence.

    Dispatches across daemon sessions, the task journal, subagent
    evidence, and live-stdout follow so the operator does not need
    to remember per-artifact paths or which command owns which
    log type. Flag precedence is ``--follow`` > ``--daemon`` >
    task-id branch, with ``--agent``/``--all`` further selecting
    inside the task branch.
    """
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
def list_tasks_command(
    workspace: WorkspaceOption = Path.cwd(),
    show_all: Annotated[bool, typer.Option("--all", help="Include done tasks")] = False,
) -> int:
    """
    Operator queue overview.

    Hides DONE tasks by default so the live work surface stays
    scannable; ``--all`` opts back into the historical view.
    Loads tasks with ``strict=False`` so a single corrupted record
    does not blank out the entire listing.
    """
    ensure_workspace(workspace)
    tasks = list_tasks(workspace, strict=False)
    filtered = []
    for task in tasks:
        if not show_all and task.status == TaskStatus.DONE:
            continue
        filtered.append(task)
    for task in filtered:
        if task.status == TaskStatus.FLAGGED:
            flag_reason = f" flag_reason={_display_flag_reason(task)}"
        else:
            flag_reason = ""
        if task.status in {TaskStatus.CLOSED, TaskStatus.DONE}:
            close_reason = f" close_reason={_display_close_reason(task)}"
        else:
            close_reason = ""
        print(f"{task.id} [{task.status}/{task.pipeline_status}] {task.title}{flag_reason}{close_reason}")
    return 0


@app.command("show", help="Print full details for a single task")
def show(task_id: Annotated[str, typer.Argument(help="Task ID")], workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Print every field for one task.

    Used by operators who need criteria, plan, runtime stage, last
    outcome, and provenance on one screen before deciding to
    retry, abandon, or edit. Distinct from ``task evidence``
    because triage rarely needs the full surface — this is the
    "I'm reasoning about the whole task" view.
    """
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
    if current_stage and current_stage.stage:
        current_stage_label = current_stage.stage
    else:
        current_stage_label = "-"
    print(f"current_stage: {current_stage_label}")
    print(f"retry_count: {pipeline_runtime.retry_count}")
    print(f"last_outcome: {pipeline_runtime.last_outcome or '-'}")
    return 0


@app.command("abandon", help="Cancel a flagged or closed task and remove it from the queue")
def abandon(task_id: Annotated[str, typer.Argument(help="Task id")], workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Operator escape hatch for tasks the pipeline cannot finish.

    Intentionally distinct from ``close`` because abandoned work
    yields no follow-up reasoning: there is no "won't do
    because…" to record, the operator just wants the task out of
    the queue. Closing with an explicit outcome is the correct
    surface when a reason and follow-up matter.
    """
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
    """
    Close a task with an explicit outcome and optional follow-up.

    Reused by planner and reviewer subagents (via env-detected
    role) so the same command surface serves operators and
    in-pipeline agents while producing different audit
    attribution. The agent path is gated through
    :func:`resolve_active_agent_task_mutation_target` so an agent
    cannot close a queued task it should not touch.
    """
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
    """
    Edit task fields after creation.

    Shared with planner and reviewer subagents so they can shape
    the active task without their own command surface; mutations
    still route through the workspace audit log so an agent's
    edits are distinguishable from an operator's. Each option is
    treated as "leave alone" when absent so partial updates do
    not stomp on unspecified fields.
    """
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

        if title is not None:
            title_arg = title
        else:
            title_arg = ...
        if priority is not None:
            priority_arg = priority
        else:
            priority_arg = ...
        if goal is not None:
            goal_arg = goal
        else:
            goal_arg = ...
        task = update_task(
            mutation_workspace,
            mutation_task_id,
            title=title_arg,
            depends_on=depends_on,
            priority=priority_arg,
            goal=goal_arg,
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
    if task.retry_policy.max_retries is not None:
        retry_limit_label = task.retry_policy.max_retries
    else:
        retry_limit_label = "default"
    print(f"retry_limit: {retry_limit_label}")
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
