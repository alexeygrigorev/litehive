"""Restricted CLI helpers for agents running inside the v2 pipeline.

When ``LITEHIVE_AGENT_ROLE`` is set, top-level ``litehive report`` is routed
through this restricted implementation. The hidden ``litehive agent ...``
command remains as a backward-compatible alias.

This module also exposes small helpers that other CLI commands can use to
distinguish operator-only surfaces from the limited agent-facing API.
"""

import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound

from litehive.config.workspace import normalize_workspace_root, resolve_workspace
from litehive.domain.reports import TaskActivityEntry, classify_task_activity_verdict
from litehive.state.records import get_task_record
from litehive.state.persist import load_state
from litehive.tasks.activity import append_task_activity


VERDICT_ALLOWLIST: dict[str, set[str]] = {
    "planner": {"pass", "reject"},
    "swe": {"pass", "reject"},
    "qa": {"pass", "reject"},
    "reviewer": {"pass", "reject"},
    "recovery": {"resume", "advance", "done", "budget_hit", "reject"},
    "merge-resolver": {"pass", "reject"},
}

agent_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=True,
)


def _current_role() -> str | None:
    return os.environ.get("LITEHIVE_AGENT_ROLE")


def current_agent_role() -> str | None:
    return _current_role()


def _current_stage() -> str | None:
    stage = os.environ.get("LITEHIVE_STAGE")
    return stage.strip() if stage else None


def _resolve_report_stage(*, explicit_stage: str | None, task, pipeline_stage: str | None) -> str:
    if explicit_stage:
        return explicit_stage
    env_stage = _current_stage()
    if env_stage:
        return env_stage
    if pipeline_stage:
        return pipeline_stage
    runtime_stage = task.runtime.current_stage.stage
    if runtime_stage:
        return runtime_stage
    return task.pipeline_status


def _allowed_verdicts_for_role(role: str) -> set[str]:
    return VERDICT_ALLOWLIST.get(role, {"pass", "reject"})


def block_if_agent() -> None:
    """Call at the top of any command agents should not use."""
    if _current_role() is not None:
        print("You are not authorized to perform this command.")
        raise SystemExit(1)


@agent_app.command("report", help="Submit your stage verdict")
def agent_report_command(
    verdict: Annotated[str, typer.Option("--verdict", help="Stage verdict")],
    message: Annotated[str, typer.Option("--message", help="Your report text (use - for stdin)")] = "",
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Read message from file")] = None,
    role: Annotated[str | None, typer.Option("--role", help="Override role (default: from env)")] = None,
    stage: Annotated[str | None, typer.Option("--stage", help="Override stage (default: from task)")] = None,
    target_stage: Annotated[
        str | None,
        typer.Option("--target-stage", help="Recovery destination stage", hidden=True),
    ] = None,
    task_id: Annotated[str | None, typer.Option("--task-id", help="Override task id")] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Repository root containing .litehive/"),
    ] = None,
    files_changed: Annotated[list[str] | None, typer.Option("--files-changed", help="Changed file paths")] = None,
    follow_up_task: Annotated[
        str | None,
        typer.Option("--follow-up-task", help="Optional follow-up task id", hidden=True),
    ] = None,
) -> None:
    if message == "-":
        message = sys.stdin.read()
    elif message_file is not None:
        message = message_file.read_text(encoding="utf-8")

    agent_role = role or _current_role()
    if not agent_role:
        print("report failed: LITEHIVE_AGENT_ROLE not set and --role not provided")
        raise SystemExit(1)

    normalized_verdict = verdict.strip().lower()
    allowed = _allowed_verdicts_for_role(agent_role)
    if normalized_verdict not in allowed:
        print("You are not authorized to perform this command.")
        raise SystemExit(1)
    normalized_target_stage = target_stage.strip() if target_stage else None
    if agent_role == "recovery" and normalized_verdict in {"resume", "advance"}:
        if not normalized_target_stage:
            print(f"report failed: recovery verdict '{normalized_verdict}' requires --target-stage")
            raise SystemExit(1)
    elif normalized_target_stage:
        print("report failed: --target-stage is only valid with recovery resume/advance verdicts")
        raise SystemExit(1)

    tid = task_id or os.environ.get("LITEHIVE_TASK_ID")
    try:
        root = (
            resolve_workspace(tid)
            if workspace is None
            else normalize_workspace_root(workspace, source="--workspace")
        )
    except ValueError as exc:
        print(f"report failed: {exc}")
        raise SystemExit(1)
    if not tid:
        state = load_state(root)
        tid = state.active_task_id
    if not tid:
        print("report failed: no task id")
        raise SystemExit(1)
    task = get_task_record(root, tid)
    if task is None:
        print(f"report failed: task {tid} not found")
        raise SystemExit(1)
    normalized_follow_up_task = follow_up_task.strip() if follow_up_task else None
    if normalized_follow_up_task:
        if normalized_follow_up_task == task.id:
            print("report failed: follow-up task id cannot reference the current task")
            raise SystemExit(1)
        if get_task_record(root, normalized_follow_up_task) is None:
            print(f"report failed: follow-up task {normalized_follow_up_task} not found")
            raise SystemExit(1)

    try:
        pipeline_state = SqlitePersistence(root).load(tid)
        pipeline_stage = pipeline_state.stage
    except TaskNotFound:
        pipeline_stage = None
    actual_stage = _resolve_report_stage(
        explicit_stage=stage,
        task=task,
        pipeline_stage=pipeline_stage,
    )
    verdict_classification = classify_task_activity_verdict(agent_role, normalized_verdict)
    entry = TaskActivityEntry(
        role=agent_role,
        stage=actual_stage,
        target_stage=normalized_target_stage,
        verdict=normalized_verdict,
        verdict_classification=verdict_classification,
        message=message,
        files_changed=list(files_changed or []),
        follow_up_task_id=normalized_follow_up_task,
    )
    append_task_activity(root, task, entry)
    print(f"task: {task.id}")
    print(f"stage: {actual_stage}")
    print(f"verdict: {normalized_verdict}")
    print(f"role: {agent_role}")
    if verdict_classification:
        print(f"verdict_classification: {verdict_classification}")
    if normalized_target_stage:
        print(f"target_stage: {normalized_target_stage}")
    if normalized_follow_up_task:
        print(f"follow_up_task: {normalized_follow_up_task}")


def _require_role(allowed: set[str]) -> str:
    """Exit if the current role is not in ``allowed``."""
    role = _current_role()
    if role is None or role not in allowed:
        print("You are not authorized to perform this command.")
        raise SystemExit(1)
    return role


def require_agent_role(allowed: set[str]) -> str:
    return _require_role(allowed)


@agent_app.command("update", help="Update task fields (planner/reviewer only)")
def agent_update_command(
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    goal: Annotated[str | None, typer.Option("--goal")] = None,
    acceptance_criteria: Annotated[list[str] | None, typer.Option("--acceptance-criteria")] = None,
    plan: Annotated[list[str] | None, typer.Option("--plan-step")] = None,
    constraints: Annotated[list[str] | None, typer.Option("--constraint")] = None,
    priority: Annotated[str | None, typer.Option("--priority")] = None,
) -> None:
    _require_role({"planner", "reviewer"})

    from litehive.tasks.status import update_task

    tid = task_id or os.environ.get("LITEHIVE_TASK_ID")
    if not tid:
        raise SystemExit(1)
    try:
        root = resolve_workspace(tid)
    except ValueError:
        raise SystemExit(1)

    sentinel = ...
    update_task(
        root,
        tid,
        goal=goal if goal is not None else sentinel,
        acceptance_criteria=acceptance_criteria if acceptance_criteria is not None else sentinel,
        plan=plan if plan is not None else sentinel,
        constraints=constraints if constraints is not None else sentinel,
        priority=priority if priority is not None else sentinel,
        allow_active_agent_task_mutation=True,
        audit_actor="agent",
        audit_source="agent",
    )
    print(f"task: {tid}")
    print("updated: ok")


@agent_app.command("close", help="Close a task (planner/reviewer only)")
def agent_close_command(
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    outcome: Annotated[str, typer.Option("--outcome", help="done, duplicate, deferred, or wont_do")] = "duplicate",
    reason: Annotated[str, typer.Option("--reason")] = "",
) -> None:
    _require_role({"planner", "reviewer"})

    from litehive.tasks.status import close_task

    tid = task_id or os.environ.get("LITEHIVE_TASK_ID")
    if not tid:
        raise SystemExit(1)
    try:
        root = resolve_workspace(tid)
    except ValueError:
        raise SystemExit(1)

    close_task(root, tid, outcome=outcome, reason=reason, audit_actor="agent", audit_source="agent")
    print(f"task: {tid}")
    print(f"outcome: {outcome}")
