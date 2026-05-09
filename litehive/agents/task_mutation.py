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
    message: str
    unauthorized: bool = False

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class AgentTaskMutationTarget:
    role: str
    workspace: Workspace
    task_id: str


@dataclass(frozen=True)
class AgentTaskUpdateRequest:
    goal: str | None = None
    acceptance_criteria: list[str] | None = None
    plan: list[str] | None = None
    constraints: list[str] | None = None
    priority: str | None = None

    def has_changes(self) -> bool:
        return (
            self.goal is not None
            or self.acceptance_criteria is not None
            or self.plan is not None
            or self.constraints is not None
            or self.priority is not None
        )


@dataclass(frozen=True)
class AgentTaskCloseRequest:
    outcome: str
    reason: str


@dataclass(frozen=True)
class AgentTaskMutationAuthorizer:
    role: str | None
    env_task_id: str | None
    env_workspace_root: str | None
    workspace_from_path: Callable[[Path], Workspace]

    def authorize(self, requested_task_id: str | None, allowed_roles: set[str]) -> AgentTaskMutationTarget:
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
        if self.role is None or self.role not in allowed_roles:
            raise AgentTaskMutationError("agent role is not authorized", unauthorized=True)
        return self.role

    def _resolve_workspace(self, task_id: str) -> Workspace:
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
    task_id: str

    def update(self, request: AgentTaskUpdateRequest) -> TaskRecord:
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
        return TaskStatusService(self.workspace).close(
            self.task_id,
            outcome=request.outcome,
            reason=request.reason,
            audit_actor="agent",
            audit_source="agent",
        )


def _normalized_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized
