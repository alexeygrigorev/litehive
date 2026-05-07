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
from pathlib import Path
from typing import Annotated

import typer

from litehive.agents.report_submission import AgentReportRequest, AgentReportSubmissionError
from litehive.agents.task_mutation import AgentTaskMutationAuthorizer, AgentTaskMutationError, AgentTaskMutationTarget
from litehive.container import build_agent_report_submitter, build_workspace
from litehive.domain.agent import SubagentId
from litehive.domain.common import Verdict

from litehive.config.workspace import normalize_workspace_root, resolve_workspace
from litehive.tasks.status import close_task_for_workspace, update_task_for_workspace
from litehive.state.persist import load_state


agent_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=True,
)


def _current_role() -> str | None:
    """
    Read the orchestrator-injected role marker.

    The orchestrator exports ``LITEHIVE_AGENT_ROLE`` when launching a
    subagent shell. CLI commands consult this to tell whether they
    are running inside an agent context vs an operator shell.
    """
    return os.environ.get("LITEHIVE_AGENT_ROLE")


def _current_subagent_id() -> SubagentId | None:
    """
    Read the orchestrator-injected subagent id.

    Used by ``agent report`` to look up the authoritative role in
    the subagent_sessions table; the env var alone cannot be trusted
    because a misbehaving agent could rewrite it.
    """
    subagent_id = os.environ.get("LITEHIVE_SUBAGENT_ID")
    if subagent_id and subagent_id.strip():
        return SubagentId(subagent_id.strip())
    return None


def current_agent_role() -> str | None:
    """
    Public read-only accessor for the active agent role.

    Used by operator commands (``task close``, ``task update``) to
    decide whether a mutation should be attributed to an agent or
    the operator in the audit trail.
    """
    return _current_role()


def _current_stage() -> str | None:
    """
    Read the orchestrator-injected stage label.

    Lets a verdict be attributed to the stage that actually launched
    the subagent, even if the task has since advanced — without it
    a slow-reporting agent could land its verdict against the wrong
    stage.
    """
    stage = os.environ.get("LITEHIVE_STAGE")
    if stage:
        return stage.strip()
    return None


def block_if_agent() -> None:
    """
    Refuse to run when invoked inside a subagent shell.

    Called at the top of any operator-only command (status, list,
    show, etc.) so an agent that escapes its prompt and tries to
    invoke an inspection command exits non-zero with the standard
    refusal text instead of leaking workspace state.
    """
    if _current_role() is not None:
        print(_agent_unauthorized_message())
        raise SystemExit(1)


def _agent_unauthorized_message() -> str:
    """
    Single source of truth for the operator-only refusal text.

    Centralized so every guarded surface refuses subagents with the
    same wording — divergent refusal messages would fragment the
    contract that agents are taught to recognize and recover from.
    """
    return (
        "You are not authorized to perform this command. "
        "PM agents may shape only the active task via "
        "`litehive agent update ...` or `litehive agent close ...`; "
        "operator inspection commands such as status/list/show are not available to agents."
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
    """
    Sole channel by which a pipeline subagent submits its stage verdict.

    Verdict allow-list and identity are resolved from the
    orchestrator-created session, not the CLI flags, so a misbehaving
    agent cannot invent verdicts outside its role. The verdict is
    appended to task activity along with files-changed and an
    optional follow-up task id; recovery resume/advance verdicts
    additionally require ``--target-stage`` because the pipeline
    cannot infer the resumption point.
    """
    if message == "-":
        message = sys.stdin.read()
    elif message_file is not None:
        message = message_file.read_text(encoding="utf-8")

    try:
        normalized_verdict = Verdict(verdict.strip().lower())
    except ValueError:
        print("You are not authorized to perform this command.")
        raise SystemExit(1)
    tid = task_id or os.environ.get("LITEHIVE_TASK_ID")
    try:
        if workspace is None:
            root = resolve_workspace(tid)
        else:
            root = normalize_workspace_root(workspace, source="--workspace")
    except ValueError as exc:
        print(f"report failed: {exc}")
        raise SystemExit(1)
    workspace_obj = build_workspace(root)
    root = workspace_obj.root
    if not tid:
        state = load_state(root)
        tid = state.active_task_id
    if not tid:
        print("report failed: no task id")
        raise SystemExit(1)
    submitter = build_agent_report_submitter(
        workspace_obj,
        env_role=_current_role(),
        env_subagent_id=_current_subagent_id(),
        env_stage=_current_stage(),
    )
    request = AgentReportRequest(
        task_id=tid,
        verdict=normalized_verdict,
        message=message,
        explicit_stage=stage,
        target_stage=target_stage,
        files_changed=list(files_changed or []),
        follow_up_task_id=follow_up_task,
    )
    try:
        submission = submitter.submit(request)
    except AgentReportSubmissionError as exc:
        if exc.unauthorized:
            print("You are not authorized to perform this command.")
        else:
            print(f"report failed: {exc}")
        raise SystemExit(1)
    print(f"task: {submission.task_id}")
    print(f"stage: {submission.stage}")
    print(f"verdict: {submission.verdict}")
    print(f"role: {submission.role}")
    print(f"source_subagent_id: {submission.source_subagent_id}")
    if submission.verdict_classification:
        print(f"verdict_classification: {submission.verdict_classification}")
    if submission.target_stage:
        print(f"target_stage: {submission.target_stage}")
    if submission.follow_up_task_id:
        print(f"follow_up_task: {submission.follow_up_task_id}")


def resolve_active_agent_task_mutation_target(
    requested_task_id: str | None,
    allowed_roles: set[str],
) -> AgentTaskMutationTarget:
    """
    Authorize and resolve the task an agent is allowed to mutate.

    Used by ``agent update`` and ``agent close``. Combines three
    checks into one place: role gate (planner/reviewer only),
    workspace resolution from ``LITEHIVE_WORKSPACE_ROOT``, and the
    "active task only" rule that prevents an agent from rewriting
    a queued task it should not touch. The active-task rule is
    enforced against the persisted state, not the env, so a stale
    ``LITEHIVE_TASK_ID`` cannot extend the agent's reach.
    """
    authorizer = AgentTaskMutationAuthorizer(
        role=_current_role(),
        env_task_id=os.environ.get("LITEHIVE_TASK_ID"),
        env_workspace_root=os.environ.get("LITEHIVE_WORKSPACE_ROOT"),
    )
    try:
        return authorizer.authorize(requested_task_id, allowed_roles)
    except AgentTaskMutationError as exc:
        if exc.unauthorized:
            print(_agent_unauthorized_message())
        else:
            print(f"agent task mutation failed: {exc}")
        raise SystemExit(1)


@agent_app.command("update", help="Update task fields (planner/reviewer only)")
def agent_update_command(
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    goal: Annotated[str | None, typer.Option("--goal")] = None,
    acceptance_criteria: Annotated[list[str] | None, typer.Option("--acceptance-criteria")] = None,
    plan: Annotated[list[str] | None, typer.Option("--plan-step")] = None,
    constraints: Annotated[list[str] | None, typer.Option("--constraint")] = None,
    priority: Annotated[str | None, typer.Option("--priority")] = None,
) -> None:
    """
    Restricted shape-the-task surface for planner/reviewer subagents.

    Mutations route through the active-task gate so an agent cannot
    quietly rewrite an unrelated queued task. Each field is treated
    as "leave alone" when its option is absent so partial updates
    do not stomp on the rest of the task; the audit trail records
    the agent role as the actor.
    """
    target = resolve_active_agent_task_mutation_target(task_id, allowed_roles={"planner", "reviewer"})

    sentinel = ...
    if goal is not None:
        goal_arg = goal
    else:
        goal_arg = sentinel
    if acceptance_criteria is not None:
        acceptance_criteria_arg = acceptance_criteria
    else:
        acceptance_criteria_arg = sentinel
    if plan is not None:
        plan_arg = plan
    else:
        plan_arg = sentinel
    if constraints is not None:
        constraints_arg = constraints
    else:
        constraints_arg = sentinel
    if priority is not None:
        priority_arg = priority
    else:
        priority_arg = sentinel
    target_workspace = build_workspace(target.root)
    update_task_for_workspace(
        target_workspace,
        target.task_id,
        goal=goal_arg,
        acceptance_criteria=acceptance_criteria_arg,
        plan=plan_arg,
        constraints=constraints_arg,
        priority=priority_arg,
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
    """
    Let a planner/reviewer subagent close the active task in flight.

    Used when an agent discovers mid-pipeline that the task should
    not run (e.g. duplicate detection by the planner) and needs to
    exit cleanly without going through the operator close path.
    The audit trail attributes the close to the agent role.
    """
    target = resolve_active_agent_task_mutation_target(task_id, allowed_roles={"planner", "reviewer"})

    target_workspace = build_workspace(target.root)
    task = close_task_for_workspace(
        target_workspace,
        target.task_id,
        outcome=outcome,
        reason=reason,
        audit_actor="agent",
        audit_source="agent",
    )
    print(f"task: {target.task_id}")
    print(f"status: {task.status}")
    print(f"close_reason: {task.close_reason or outcome}")
