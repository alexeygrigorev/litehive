from pathlib import Path

import pytest

from litehive.agents.task_mutation import AgentTaskMutationAuthorizer, AgentTaskMutationError
from litehive.config.workspace import ensure_workspace
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task


def _set_active_task(root: Path, task_id: str) -> None:
    state = load_state(root)
    state.active_task_id = task_id
    save_state(root, state)


def test_agent_task_mutation_authorizer_resolves_active_env_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Active task")
    _set_active_task(tmp_path, task.id)
    authorizer = AgentTaskMutationAuthorizer(
        role="planner",
        env_task_id=task.id,
        env_workspace_root=str(tmp_path),
    )

    target = authorizer.authorize(requested_task_id=None, allowed_roles={"planner", "reviewer"})

    assert target.role == "planner"
    assert target.root == tmp_path.resolve()
    assert target.task_id == task.id


def test_agent_task_mutation_authorizer_rejects_inactive_requested_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    queued = create_task(tmp_path, title="Queued task")
    _set_active_task(tmp_path, active.id)
    authorizer = AgentTaskMutationAuthorizer(
        role="planner",
        env_task_id=active.id,
        env_workspace_root=str(tmp_path),
    )

    with pytest.raises(AgentTaskMutationError, match=f"agents may only mutate active task {active.id}, not {queued.id}"):
        authorizer.authorize(requested_task_id=queued.id, allowed_roles={"planner", "reviewer"})


def test_agent_task_mutation_authorizer_marks_role_rejections_as_unauthorized(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Active task")
    _set_active_task(tmp_path, task.id)
    authorizer = AgentTaskMutationAuthorizer(
        role="swe",
        env_task_id=task.id,
        env_workspace_root=str(tmp_path),
    )

    with pytest.raises(AgentTaskMutationError) as excinfo:
        authorizer.authorize(requested_task_id=None, allowed_roles={"planner", "reviewer"})

    assert excinfo.value.unauthorized
