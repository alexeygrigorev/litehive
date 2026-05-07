"""Authorization service for agent-initiated task mutations."""

from dataclasses import dataclass
from pathlib import Path

from litehive.config.workspace import normalize_workspace_root, resolve_workspace
from litehive.state.persist import load_state


@dataclass(frozen=True)
class AgentTaskMutationError(Exception):
    message: str
    unauthorized: bool = False

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class AgentTaskMutationTarget:
    role: str
    root: Path
    task_id: str


@dataclass(frozen=True)
class AgentTaskMutationAuthorizer:
    role: str | None
    env_task_id: str | None
    env_workspace_root: str | None

    def authorize(self, requested_task_id: str | None, allowed_roles: set[str]) -> AgentTaskMutationTarget:
        role = self._authorized_role(allowed_roles)
        env_task_id = _normalized_optional(self.env_task_id)
        task_id = requested_task_id or env_task_id
        if task_id is None:
            raise AgentTaskMutationError("LITEHIVE_TASK_ID is not set")

        root = self._resolve_root(task_id)
        state = load_state(root)
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
        return AgentTaskMutationTarget(role=role, root=root, task_id=task_id)

    def _authorized_role(self, allowed_roles: set[str]) -> str:
        if self.role is None or self.role not in allowed_roles:
            raise AgentTaskMutationError("agent role is not authorized", unauthorized=True)
        return self.role

    def _resolve_root(self, task_id: str) -> Path:
        env_workspace_root = _normalized_optional(self.env_workspace_root)
        try:
            if env_workspace_root is not None:
                return normalize_workspace_root(Path(env_workspace_root), source="LITEHIVE_WORKSPACE_ROOT")
            return resolve_workspace(task_id)
        except ValueError as exc:
            raise AgentTaskMutationError(str(exc)) from exc


def _normalized_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized
