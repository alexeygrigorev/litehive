"""Restricted CLI helpers for agents running inside the pipeline.

``litehive agent report`` is the restricted reporting entrypoint for pipeline
agents. Report submissions resolve the role
from the orchestrator-created subagent session instead of trusting a CLI flag.
The hidden ``litehive agent ...`` command is the internal entrypoint for
agent-scoped commands.

This module also exposes small helpers that other CLI commands can use to
distinguish operator-only surfaces from the limited agent-facing API.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from litehive.agents.session_store import load_subagent_session
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


def _current_subagent_id() -> str | None:
    subagent_id = os.environ.get("LITEHIVE_SUBAGENT_ID")
    return subagent_id.strip() if subagent_id and subagent_id.strip() else None


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
    runtime_stage = task.runtime.pipeline.current_stage.stage
    if runtime_stage:
        return runtime_stage
    return task.pipeline_status


def _allowed_verdicts_for_role(role: str) -> set[str]:
    return VERDICT_ALLOWLIST.get(role, {"pass", "reject"})


@dataclass(frozen=True)
class AgentReportIdentity:
    role: str
    subagent_id: str


def _resolve_report_identity(root: Path, task) -> AgentReportIdentity:
    subagent_id = _current_subagent_id()
    if subagent_id is None:
        print("report failed: LITEHIVE_SUBAGENT_ID not set")
        raise SystemExit(1)

    session = load_subagent_session(root, task.id, subagent_id)
    if not session:
        print(f"report failed: subagent session {subagent_id} not found for task {task.id}")
        raise SystemExit(1)

    payload_id = session.get("id")
    if isinstance(payload_id, str) and payload_id and payload_id != subagent_id:
        print(f"report failed: subagent session id mismatch for {subagent_id}")
        raise SystemExit(1)

    role = session.get("role")
    if not isinstance(role, str) or not role.strip():
        print(f"report failed: subagent session {subagent_id} has no role")
        raise SystemExit(1)
    role = role.strip()

    env_role = _current_role()
    if env_role and env_role != role:
        print("report failed: LITEHIVE_AGENT_ROLE does not match subagent session identity")
        raise SystemExit(1)

    return AgentReportIdentity(role=role, subagent_id=subagent_id)


def block_if_agent() -> None:
    """Call at the top of any command agents should not use."""
    if _current_role() is not None:
        print(_agent_unauthorized_message())
        raise SystemExit(1)


def _agent_unauthorized_message() -> str:
    return (
        "You are not authorized to perform this command. "
        "PM agents may shape only the active task via "
        "`litehive agent update ...` or `litehive agent close ...`; "
        "operator inspection commands such as status/list/browse/show are not available to agents."
    )


@agent_app.command("report", help="Submit your stage verdict")
def agent_report_command(
    verdict: Annotated[str, typer.Option("--verdict", help="Stage verdict")],
    message: Annotated[str, typer.Option("--message", help="Your report text (use - for stdin)")] = "",
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Read message from file")] = None,
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

    normalized_verdict = verdict.strip().lower()
    tid = task_id or os.environ.get("LITEHIVE_TASK_ID")
    try:
        root = (
            resolve_workspace(tid) if workspace is None else normalize_workspace_root(workspace, source="--workspace")
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
    identity = _resolve_report_identity(root, task)
    agent_role = identity.role

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
        source_subagent_id=identity.subagent_id,
        follow_up_task_id=normalized_follow_up_task,
    )
    append_task_activity(root, task, entry)
    print(f"task: {task.id}")
    print(f"stage: {actual_stage}")
    print(f"verdict: {normalized_verdict}")
    print(f"role: {agent_role}")
    print(f"source_subagent_id: {identity.subagent_id}")
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
        print(_agent_unauthorized_message())
        raise SystemExit(1)
    return role


def require_agent_role(allowed: set[str]) -> str:
    return _require_role(allowed)


@dataclass(frozen=True)
class AgentTaskMutationTarget:
    role: str
    root: Path
    task_id: str


def resolve_active_agent_task_mutation_target(
    requested_task_id: str | None,
    *,
    allowed_roles: set[str],
) -> AgentTaskMutationTarget:
    """Authorize and resolve a PM mutation target through the source workspace."""
    role = _require_role(allowed_roles)
    env_task_id = os.environ.get("LITEHIVE_TASK_ID")
    env_task_id = env_task_id.strip() if env_task_id and env_task_id.strip() else None
    tid = requested_task_id or env_task_id
    if not tid:
        print("agent task mutation failed: LITEHIVE_TASK_ID is not set")
        raise SystemExit(1)
    try:
        env_workspace = os.environ.get("LITEHIVE_WORKSPACE_ROOT")
        if env_workspace and env_workspace.strip():
            root = normalize_workspace_root(Path(env_workspace), source="LITEHIVE_WORKSPACE_ROOT")
        else:
            root = resolve_workspace(tid)
    except ValueError as exc:
        print(f"agent task mutation failed: {exc}")
        raise SystemExit(1)
    state = load_state(root)
    if (
        env_task_id is not None
        and state.active_task_id == env_task_id
        and requested_task_id is not None
        and requested_task_id != env_task_id
    ):
        print(f"agent task mutation failed: agents may only mutate active task {env_task_id}, not {requested_task_id}")
        raise SystemExit(1)
    if state.active_task_id != tid:
        active = state.active_task_id or "-"
        print(f"agent task mutation failed: agents may only mutate active task {active}, not {tid}")
        raise SystemExit(1)
    return AgentTaskMutationTarget(role=role, root=root, task_id=tid)


@agent_app.command("update", help="Update task fields (planner/reviewer only)")
def agent_update_command(
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    goal: Annotated[str | None, typer.Option("--goal")] = None,
    acceptance_criteria: Annotated[list[str] | None, typer.Option("--acceptance-criteria")] = None,
    plan: Annotated[list[str] | None, typer.Option("--plan-step")] = None,
    constraints: Annotated[list[str] | None, typer.Option("--constraint")] = None,
    priority: Annotated[str | None, typer.Option("--priority")] = None,
) -> None:
    from litehive.tasks.status import update_task

    target = resolve_active_agent_task_mutation_target(task_id, allowed_roles={"planner", "reviewer"})

    sentinel = ...
    update_task(
        target.root,
        target.task_id,
        goal=goal if goal is not None else sentinel,
        acceptance_criteria=acceptance_criteria if acceptance_criteria is not None else sentinel,
        plan=plan if plan is not None else sentinel,
        constraints=constraints if constraints is not None else sentinel,
        priority=priority if priority is not None else sentinel,
        allow_active_agent_task_mutation=True,
        audit_actor="agent",
        audit_source="agent",
    )
    print(f"task: {target.task_id}")
    print("updated: ok")


@agent_app.command("close", help="Close a task (planner/reviewer only)")
def agent_close_command(
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    outcome: Annotated[
        str, typer.Option("--outcome", help="Close reason: done, duplicate, deferred, or wont_do")
    ] = "duplicate",
    reason: Annotated[str, typer.Option("--reason")] = "",
) -> None:
    from litehive.tasks.status import close_task

    target = resolve_active_agent_task_mutation_target(task_id, allowed_roles={"planner", "reviewer"})

    task = close_task(
        target.root,
        target.task_id,
        outcome=outcome,
        reason=reason,
        audit_actor="agent",
        audit_source="agent",
    )
    print(f"task: {target.task_id}")
    print(f"status: {task.status}")
    print(f"close_reason: {task.close_reason or outcome}")
