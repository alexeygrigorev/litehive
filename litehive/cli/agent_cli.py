"""Restricted CLI for agents running inside the v2 pipeline.

When ``LITEHIVE_AGENT_ROLE`` is set, agents use ``litehive agent report``
instead of ``litehive report``. The verdict options are restricted per role
so agents literally cannot submit verdicts they're not allowed to use.

Also provides a guard function ``block_if_agent()`` that other CLI
commands call at the top to prevent agents from using them.
"""

import os
from pathlib import Path
from typing import Annotated

import typer

from litehive.pipeline.persistence import SqlitePersistence, TaskNotFound

from litehive.config import resolve_workspace
from litehive.models import TaskThreadComment
from litehive.tasks.crud import get_task
from litehive.tasks.persistence import load_state
from litehive.tasks.reports import append_thread_comment


VERDICT_ALLOWLIST: dict[str, set[str]] = {
    "planner": {"pass", "reject", "blocked"},
    "swe": {"pass", "blocked"},
    "qa": {"pass", "reject", "blocked"},
    "reviewer": {"pass", "reject", "blocked"},
    "recovery": {"pass", "reject", "blocked"},
    "merge-resolver": {"pass", "reject", "blocked"},
}

agent_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=True,
)


def _current_role() -> str | None:
    return os.environ.get("LITEHIVE_AGENT_ROLE")


def block_if_agent() -> None:
    """Call at the top of any command agents should not use."""
    if _current_role() is not None:
        print("You are not authorized to perform this command.")
        raise typer.Exit(1)


@agent_app.command("report", help="Submit your stage verdict")
def agent_report_command(
    verdict: Annotated[str, typer.Option("--verdict", help="pass, reject, or blocked")],
    message: Annotated[str, typer.Option("--message", help="Your report text")] = "",
    role: Annotated[str | None, typer.Option("--role", help="Override role (default: from env)")] = None,
    step: Annotated[str | None, typer.Option("--step", help="Override step (default: from task)")] = None,
    task_id: Annotated[str | None, typer.Option("--task-id", help="Override task id")] = None,
    workspace: Annotated[Path, typer.Option("--workspace", help="Workspace root")] = Path.cwd(),
    files_changed: Annotated[list[str] | None, typer.Option("--files-changed", help="Changed file paths")] = None,
) -> None:
    agent_role = role or _current_role()
    if not agent_role:
        print("report failed: LITEHIVE_AGENT_ROLE not set and --role not provided")
        raise typer.Exit(1)

    normalized_verdict = "reject" if verdict == "fail" else verdict.strip().lower()

    allowed = VERDICT_ALLOWLIST.get(agent_role, {"pass", "reject", "blocked"})
    if normalized_verdict not in allowed:
        print("You are not authorized to perform this command.")
        raise typer.Exit(1)

    tid = task_id or os.environ.get("LITEHIVE_TASK_ID")
    try:
        root = resolve_workspace(tid, workspace=workspace)
    except ValueError as exc:
        print(f"report failed: {exc}")
        raise typer.Exit(1)
    if not tid:
        state = load_state(root)
        tid = state.active_task_id
    if not tid:
        print("report failed: no task id")
        raise typer.Exit(1)
    task = get_task(root, tid)
    if task is None:
        print(f"report failed: task {tid} not found")
        raise typer.Exit(1)

    try:
        pipeline_state = SqlitePersistence(root).load(tid)
        actual_step = step or pipeline_state.stage
    except TaskNotFound:
        actual_step = step or task.pipeline_status
    comment = TaskThreadComment(
        role=agent_role,
        step=actual_step,
        verdict=normalized_verdict,
        message=message,
        files_changed=list(files_changed or []),
    )
    append_thread_comment(root, task, comment)
    print(f"task: {task.id}")
    print(f"step: {actual_step}")
    print(f"verdict: {normalized_verdict}")
    print(f"role: {agent_role}")


def _require_role(allowed: set[str]) -> str:
    """Exit if the current role is not in ``allowed``."""
    role = _current_role()
    if role is None or role not in allowed:
        print("You are not authorized to perform this command.")
        raise typer.Exit(1)
    return role


@agent_app.command("update", help="Update task fields (planner/reviewer only)")
def agent_update_command(
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path.cwd(),
    goal: Annotated[str | None, typer.Option("--goal")] = None,
    acceptance_criteria: Annotated[list[str] | None, typer.Option("--acceptance-criteria")] = None,
    plan: Annotated[list[str] | None, typer.Option("--plan-step")] = None,
    constraints: Annotated[list[str] | None, typer.Option("--constraint")] = None,
    pm_complexity: Annotated[str | None, typer.Option("--pm-complexity")] = None,
    planned_effort: Annotated[str | None, typer.Option("--planned-effort")] = None,
    priority: Annotated[str | None, typer.Option("--priority")] = None,
) -> None:
    _require_role({"planner", "reviewer"})

    from litehive.workspace.task_status import update_task

    tid = task_id or os.environ.get("LITEHIVE_TASK_ID")
    if not tid:
        raise typer.Exit(1)
    try:
        root = resolve_workspace(tid, workspace=workspace)
    except ValueError:
        raise typer.Exit(1)

    sentinel = ...
    update_task(
        root,
        tid,
        goal=goal if goal is not None else sentinel,
        acceptance_criteria=acceptance_criteria if acceptance_criteria is not None else sentinel,
        plan=plan if plan is not None else sentinel,
        constraints=constraints if constraints is not None else sentinel,
        pm_complexity=pm_complexity if pm_complexity is not None else sentinel,
        planned_effort=planned_effort if planned_effort is not None else sentinel,
        priority=priority if priority is not None else sentinel,
    )
    print(f"task: {tid}")
    print("updated: ok")


@agent_app.command("close", help="Close a task (planner/reviewer only)")
def agent_close_command(
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path.cwd(),
    outcome: Annotated[str, typer.Option("--outcome", help="duplicate, deferred, or wont_do")] = "duplicate",
    reason: Annotated[str, typer.Option("--reason")] = "",
) -> None:
    _require_role({"planner", "reviewer"})

    from litehive.workspace.task_status import close_task

    tid = task_id or os.environ.get("LITEHIVE_TASK_ID")
    if not tid:
        raise typer.Exit(1)
    try:
        root = resolve_workspace(tid, workspace=workspace)
    except ValueError:
        raise typer.Exit(1)

    close_task(root, tid, outcome=outcome, reason=reason)
    print(f"task: {tid}")
    print(f"outcome: {outcome}")
