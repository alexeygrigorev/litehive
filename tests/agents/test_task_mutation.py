from pathlib import Path

import pytest

from litehive.agents.task_mutation import (
    AgentTaskCloseRequest,
    AgentTaskMutationAuthorizer,
    AgentTaskMutationError,
    AgentTaskMutator,
    AgentTaskUpdateRequest,
)
from litehive.config.workspace import create_workspace
from litehive.state.records import WorkspaceTasks
from litehive.tasks.audit import load_task_audit_entries
from litehive.tasks.queue import TaskQueueService
from litehive.workspace import Workspace


def _mark_active_task(workspace: Workspace, task_id: str) -> None:
    TaskQueueService(workspace).mark_active(task_id)


def test_agent_task_mutation_authorizer_resolves_active_env_task(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Active task")
    _mark_active_task(workspace, task.id)
    authorizer = AgentTaskMutationAuthorizer(
        role="planner",
        env_task_id=task.id,
        env_workspace_root=str(tmp_path),
        workspace_from_path=Workspace.from_path,
    )

    target = authorizer.authorize(requested_task_id=None, allowed_roles={"planner", "reviewer"})

    assert target.role == "planner"
    assert target.workspace == Workspace.from_path(tmp_path)
    assert target.task_id == task.id


def test_agent_task_mutation_authorizer_rejects_inactive_requested_task(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    active = WorkspaceTasks(workspace).create( title="Active task")
    queued = WorkspaceTasks(workspace).create( title="Queued task")
    _mark_active_task(workspace, active.id)
    authorizer = AgentTaskMutationAuthorizer(
        role="planner",
        env_task_id=active.id,
        env_workspace_root=str(tmp_path),
        workspace_from_path=Workspace.from_path,
    )

    with pytest.raises(AgentTaskMutationError, match=f"agents may only mutate active task {active.id}, not {queued.id}"):
        authorizer.authorize(requested_task_id=queued.id, allowed_roles={"planner", "reviewer"})


def test_agent_task_mutation_authorizer_marks_role_rejections_as_unauthorized(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Active task")
    _mark_active_task(workspace, task.id)
    authorizer = AgentTaskMutationAuthorizer(
        role="swe",
        env_task_id=task.id,
        env_workspace_root=str(tmp_path),
        workspace_from_path=Workspace.from_path,
    )

    with pytest.raises(AgentTaskMutationError) as excinfo:
        authorizer.authorize(requested_task_id=None, allowed_roles={"planner", "reviewer"})

    assert excinfo.value.unauthorized


def test_agent_task_mutator_updates_active_task_with_agent_attribution(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Active task", goal="old goal")
    _mark_active_task(workspace, task.id)

    mutator = AgentTaskMutator(workspace, task.id)
    updated = mutator.update(
        AgentTaskUpdateRequest(
            goal="new goal",
            acceptance_criteria=["one boundary"],
        )
    )

    assert updated.goal == "new goal"
    assert updated.acceptance_criteria == ["one boundary"]
    persisted = WorkspaceTasks(workspace).get_record(task.id)
    assert persisted is not None
    assert persisted.goal == "new goal"
    audit_entries = load_task_audit_entries(workspace, task_id=task.id, limit=1)
    assert audit_entries[-1].actor == "agent"
    assert audit_entries[-1].source == "agent"


def test_agent_task_mutator_rejects_empty_update(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Active task")

    mutator = AgentTaskMutator(workspace, task.id)

    with pytest.raises(AgentTaskMutationError, match="no changes requested"):
        mutator.update(AgentTaskUpdateRequest())


def test_agent_task_mutator_closes_active_task_with_agent_attribution(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Duplicate task")
    _mark_active_task(workspace, task.id)

    mutator = AgentTaskMutator(workspace, task.id)
    closed = mutator.close(AgentTaskCloseRequest(outcome="duplicate", reason="covered elsewhere"))

    assert closed.status == "closed"
    assert closed.close_reason == "duplicate"
    audit_entries = load_task_audit_entries(workspace, task_id=task.id, limit=1)
    assert audit_entries[-1].actor == "agent"
    assert audit_entries[-1].source == "agent"
