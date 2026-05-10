"""Authorization service for agent-initiated task mutations."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from litehive.config.workspace import normalize_workspace_root, resolve_workspace
from litehive.domain.task import TaskRecord
from litehive.state.persist import WorkspaceStateRepository
from litehive.tasks.status import TaskStatusService
from litehive.workspace import Workspace


@dataclass(frozen=True)
class AgentTaskMutationError(Exception):
    """
    Domain error for agent task mutation validation failures.

    Carries an ``unauthorized`` flag so the CLI can distinguish
    permission errors from general validation errors.
    """

    message: str
    """Human-readable description of the validation failure."""

    unauthorized: bool = False
    """True when the agent's role is not in the allowed set."""

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class AgentTaskMutationTarget:
    """
    Resolved authorization context for a permitted task mutation.

    Returned by ``AgentTaskMutationAuthorizer.authorize`` after the
    caller's role, workspace, and task have been validated.
    """

    role: str
    """Authorized agent role extracted from the environment."""

    workspace: Workspace
    """Resolved workspace that owns the target task."""

    task_id: str
    """Task id that the agent is authorized to mutate."""


@dataclass(frozen=True)
class AgentTaskUpdateRequest:
    """
    Partial update fields supplied by the ``litehive agent task update`` CLI.

    All fields are optional; ``None`` means "do not change this field".
    ``has_changes`` returns True when at least one field is set.
    """

    goal: str | None = None
    """New task goal text, or None to keep the existing value."""

    acceptance_criteria: list[str] | None = None
    """Replacement acceptance criteria list, or None to keep existing."""

    plan: list[str] | None = None
    """Replacement plan steps, or None to keep existing."""

    constraints: list[str] | None = None
    """Replacement constraints, or None to keep existing."""

    priority: str | None = None
    """New priority label, or None to keep existing."""

    def has_changes(self) -> bool:
        """
        Return True when at least one field is set.

        Used as a guard before calling the task status service so empty
        update requests are rejected early.
        """
        return (
            self.goal is not None
            or self.acceptance_criteria is not None
            or self.plan is not None
            or self.constraints is not None
            or self.priority is not None
        )


@dataclass(frozen=True)
class AgentTaskCloseRequest:
    """
    Fields required to close a task from an agent command.

    Both outcome and reason are mandatory because the activity audit
    needs a complete record of why the task was terminated.
    """

    outcome: str
    """Canonical outcome label for the closed task."""

    reason: str
    """Free-text explanation of why the task was closed."""


@dataclass(frozen=True)
class AgentTaskMutationAuthorizer:
    """
    Verifies that an in-pipeline agent may mutate a task.

    The authorizer cross-checks the agent's role against an allow-list,
    resolves the workspace from environment or task id, and ensures the
    target task is the workspace's active task. This keeps the hidden
    agent CLI commands thin — they delegate all authorization here.
    """

    role: str | None
    """Agent role from ``LITEHIVE_AGENT_ROLE``."""

    env_task_id: str | None
    """Task id from ``LITEHIVE_TASK_ID``."""

    env_workspace_root: str | None
    """Workspace root path from ``LITEHIVE_WORKSPACE_ROOT``."""

    workspace_from_path: Callable[[Path], Workspace]
    """Factory that resolves a Path into a Workspace instance."""

    def authorize(self, requested_task_id: str | None, allowed_roles: set[str]) -> AgentTaskMutationTarget:
        """
        Validate and return the mutation target, or raise on failure.

        Checks the role against ``allowed_roles``, resolves the effective
        task id from the request or environment, and ensures the agent is
        targeting only the workspace's active task.
        """
        role = self._authorized_role(allowed_roles)
        env_task_id = _normalized_optional(self.env_task_id)
        task_id = requested_task_id or env_task_id
        if task_id is None:
            raise AgentTaskMutationError("LITEHIVE_TASK_ID is not set")

        workspace = self._resolve_workspace(task_id)
        state = WorkspaceStateRepository(workspace).load()
        if (
            env_task_id is not None
            and state.active_task_id == env_task_id
            and requested_task_id is not None
            and requested_task_id != env_task_id
        ):
            raise AgentTaskMutationError(f"agents may only mutate active task {env_task_id}, not {requested_task_id}")
        if state.active_task_id != task_id:
            active_task_id = state.active_task_id or "-"
            raise AgentTaskMutationError(f"agents may only mutate active task {active_task_id}, not {task_id}")
        return AgentTaskMutationTarget(role=role, workspace=workspace, task_id=task_id)

    def _authorized_role(self, allowed_roles: set[str]) -> str:
        """
        Return the role if it is in the allowed set, or raise unauthorized.

        Used to enforce role-based access control on agent mutation
        commands.
        """
        if self.role is None or self.role not in allowed_roles:
            raise AgentTaskMutationError("agent role is not authorized", unauthorized=True)
        return self.role

    def _resolve_workspace(self, task_id: str) -> Workspace:
        """
        Resolve the workspace from the environment root or the task id.

        Prefers ``LITEHIVE_WORKSPACE_ROOT`` when set; otherwise falls back
        to ``resolve_workspace`` which looks up the workspace that owns
        the given task.
        """
        env_workspace_root = _normalized_optional(self.env_workspace_root)
        try:
            if env_workspace_root is not None:
                root = normalize_workspace_root(Path(env_workspace_root), source="LITEHIVE_WORKSPACE_ROOT")
            else:
                root = resolve_workspace(task_id)
            return self.workspace_from_path(root)
        except ValueError as exc:
            raise AgentTaskMutationError(str(exc)) from exc


@dataclass(frozen=True)
class AgentTaskMutator:
    """
    Applies authorized task mutations requested by an in-pipeline agent.

    The CLI owns parsing Typer options and environment. This service owns
    the task-service calls and the agent audit attribution so the hidden
    agent commands stay thin.
    """

    workspace: Workspace
    """Workspace containing the target task."""

    task_id: str
    """Id of the task to mutate."""

    def update(self, request: AgentTaskUpdateRequest) -> TaskRecord:
        """
        Apply partial field updates to a task.

        Uses the sentinel pattern (Ellipsis) to distinguish "not supplied"
        from "set to None" when forwarding to ``TaskStatusService.update``.
        """
        if not request.has_changes():
            raise AgentTaskMutationError("no changes requested")
        sentinel = ...
        if request.goal is not None:
            goal = request.goal
        else:
            goal = sentinel
        if request.acceptance_criteria is not None:
            acceptance_criteria = request.acceptance_criteria
        else:
            acceptance_criteria = sentinel
        if request.plan is not None:
            plan = request.plan
        else:
            plan = sentinel
        if request.constraints is not None:
            constraints = request.constraints
        else:
            constraints = sentinel
        if request.priority is not None:
            priority = request.priority
        else:
            priority = sentinel
        return TaskStatusService(self.workspace).update(
            self.task_id,
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            plan=plan,
            constraints=constraints,
            priority=priority,
            allow_active_agent_task_mutation=True,
            audit_actor="agent",
            audit_source="agent",
        )

    def close(self, request: AgentTaskCloseRequest) -> TaskRecord:
        """
        Close the task with an outcome and reason attributed to the agent.

        Delegates to ``TaskStatusService.close`` with agent audit metadata.
        """
        return TaskStatusService(self.workspace).close(
            self.task_id,
            outcome=request.outcome,
            reason=request.reason,
            audit_actor="agent",
            audit_source="agent",
        )


def _normalized_optional(value: str | None) -> str | None:
    """
    Convert blank or whitespace-only strings to ``None``.

    CLI optional string fields arrive as empty strings when the user
    omits the option; normalizing them to ``None`` keeps downstream
    checks simple.
    """
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized
