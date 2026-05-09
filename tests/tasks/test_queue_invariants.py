from pathlib import Path

import pytest

from litehive.config.workspace import create_workspace
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import (
    WorkspaceTasks,
        )
from litehive.tasks.queue import TaskQueueService
from litehive.workspace import Workspace
from litehive.domain.common import PipelineStatus, TaskStatus


def _persist_task_status(workspace: Workspace, task_id: str, *, status: str, pipeline_status: str) -> None:
    task = WorkspaceTasks(workspace).require(task_id)
    task.status = TaskStatus(status)
    task.pipeline_status = PipelineStatus(pipeline_status)
    if status == "interrupted":
        task.runtime.pipeline.execution_status = "interrupted"
    WorkspaceTasks(workspace).save(task)


def _persist_resumable_task(workspace: Workspace, task_id: str, *, status: str, pipeline_status: str) -> None:
    task = WorkspaceTasks(workspace).require(task_id)
    task.status = TaskStatus(status)
    task.pipeline_status = PipelineStatus(pipeline_status)
    task.runtime.pipeline.current_stage.stage = pipeline_status
    if status == "interrupted":
        task.runtime.pipeline.execution_status = "interrupted"
        task.runtime.pipeline.current_stage.status = "interrupted"
    else:
        task.runtime.pipeline.execution_status = "idle"
        task.runtime.pipeline.current_stage.status = "idle"
    WorkspaceTasks(workspace).save(task)


def test_dequeue_next_task_reclaims_missing_in_progress_task_before_handoff(
    tmp_path: Path,
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    unfinished = WorkspaceTasks(workspace).create( title="Unfinished active task")
    later = WorkspaceTasks(workspace).create( title="Later queued task")
    _persist_task_status(
        workspace,
        unfinished.id,
        status="in_progress",
        pipeline_status="implementing",
    )
    later_task = WorkspaceTasks(workspace).require(later.id)
    later_task.priority = "high"
    WorkspaceTasks(workspace).save(later_task)

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = None
    state.queue = [later.id]
    WorkspaceStateRepository(workspace).save(state)

    selection = TaskQueueService(workspace).select_next()

    assert selection.task is not None
    assert selection.task.id == unfinished.id

    repaired_state = WorkspaceStateRepository(workspace).load()
    assert repaired_state.active_task_id == unfinished.id
    assert repaired_state.queue == [later.id]


def test_dequeue_next_task_ignores_stale_active_marker_when_reclaiming_missing_work(
    tmp_path: Path,
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    stale = WorkspaceTasks(workspace).create( title="Stale active task")
    unfinished = WorkspaceTasks(workspace).create( title="Unfinished active task")
    later = WorkspaceTasks(workspace).create( title="Later queued task")
    _persist_task_status(
        workspace,
        stale.id,
        status="done",
        pipeline_status="done",
    )
    _persist_task_status(
        workspace,
        unfinished.id,
        status="in_progress",
        pipeline_status="implementing",
    )

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = stale.id
    state.queue = [later.id]
    WorkspaceStateRepository(workspace).save(state)

    selection = TaskQueueService(workspace).select_next()

    assert selection.task is not None
    assert selection.task.id == unfinished.id

    repaired_state = WorkspaceStateRepository(workspace).load()
    assert repaired_state.active_task_id == unfinished.id
    assert repaired_state.queue == [later.id]


@pytest.mark.parametrize(
    ("status", "pipeline_status"),
    [
        ("queued", "implementing"),
        ("interrupted", "testing"),
    ],
    ids=["resumed", "interrupted"],
)
def test_dequeue_next_task_reclaims_missing_resumable_work_before_handoff(
    tmp_path: Path,
    status: str,
    pipeline_status: str,
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    unfinished = WorkspaceTasks(workspace).create( title="Unfinished task")
    later = WorkspaceTasks(workspace).create( title="Later queued task")
    _persist_resumable_task(
        workspace,
        unfinished.id,
        status=status,
        pipeline_status=pipeline_status,
    )

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = None
    state.queue = [later.id]
    WorkspaceStateRepository(workspace).save(state)

    selection = TaskQueueService(workspace).select_next()

    assert selection.task is not None
    assert selection.task.id == unfinished.id

    repaired_state = WorkspaceStateRepository(workspace).load()
    assert repaired_state.active_task_id == unfinished.id
    assert repaired_state.queue == [later.id]


@pytest.mark.parametrize(
    ("status", "pipeline_status"),
    [
        ("queued", "implementing"),
        ("interrupted", "testing"),
    ],
    ids=["resumed", "interrupted"],
)
def test_peek_next_task_restores_missing_resumable_tasks_ahead_of_later_queue_on_restart(
    tmp_path: Path,
    status: str,
    pipeline_status: str,
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    unfinished = WorkspaceTasks(workspace).create( title="Unfinished task")
    later = WorkspaceTasks(workspace).create( title="Later queued task")
    _persist_resumable_task(
        workspace,
        unfinished.id,
        status=status,
        pipeline_status=pipeline_status,
    )

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = None
    state.queue = [later.id]
    WorkspaceStateRepository(workspace).save(state)

    selection = TaskQueueService(workspace).peek_next_selection()

    assert selection.task is not None
    assert selection.task.id == unfinished.id

    repaired_state = WorkspaceStateRepository(workspace).load()
    assert repaired_state.active_task_id is None
    assert repaired_state.queue == [unfinished.id, later.id]


def test_dequeue_persistence_keeps_restored_queue_additions(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    first = WorkspaceTasks(workspace).create( title="First missing resumable task")
    second = WorkspaceTasks(workspace).create( title="Second missing resumable task")
    later = WorkspaceTasks(workspace).create( title="Later queued task")

    for task_id in (first.id, second.id):
        _persist_resumable_task(
            workspace,
            task_id,
            status="queued",
            pipeline_status="implementing",
        )

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = None
    state.queue = [later.id]
    WorkspaceStateRepository(workspace).save(state)

    selection = TaskQueueService(workspace).select_next()

    assert selection.task is not None
    assert selection.task.id == first.id

    repaired_state = WorkspaceStateRepository(workspace).load()
    assert repaired_state.active_task_id == first.id
    assert repaired_state.queue == [second.id, later.id]


def test_peek_canonicalizes_nonrunning_resumable_tasks_on_restart(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    stranded = WorkspaceTasks(workspace).create( title="Stranded in progress")
    resumed = WorkspaceTasks(workspace).create( title="Interrupted resumable task")

    _persist_task_status(
        workspace,
        stranded.id,
        status="in_progress",
        pipeline_status="testing",
    )
    stranded_task = WorkspaceTasks(workspace).require(stranded.id)
    stranded_task.runtime.pipeline.execution_status = "idle"
    stranded_task.runtime.pipeline.current_stage.stage = "testing"
    stranded_task.runtime.pipeline.current_stage.status = "idle"
    WorkspaceTasks(workspace).save(stranded_task)

    _persist_task_status(
        workspace,
        resumed.id,
        status="interrupted",
        pipeline_status="implementing",
    )

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = None
    state.queue = []
    WorkspaceStateRepository(workspace).save(state)

    selection = TaskQueueService(workspace).peek_next_selection()

    assert selection.task is not None

    repaired_state = WorkspaceStateRepository(workspace).load()
    assert repaired_state.active_task_id is None
    assert set(repaired_state.queue) == {stranded.id, resumed.id}

    refreshed_stranded = WorkspaceTasks(workspace).require(stranded.id)
    assert refreshed_stranded.status == "queued"
    assert refreshed_stranded.pipeline_status == "testing"
    assert refreshed_stranded.runtime.pipeline.execution_status == "idle"
    assert refreshed_stranded.runtime.pipeline.current_stage.stage == "testing"
    assert refreshed_stranded.runtime.pipeline.current_stage.status == "idle"

    refreshed_resumed = WorkspaceTasks(workspace).require(resumed.id)
    assert refreshed_resumed.status == "queued"
    assert refreshed_resumed.pipeline_status == "implementing"
    assert refreshed_resumed.runtime.pipeline.execution_status == "idle"
    assert refreshed_resumed.runtime.pipeline.current_stage.stage == "implementing"
    assert refreshed_resumed.runtime.pipeline.current_stage.status == "idle"


def test_done_dependency_satisfies_queued_task(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    dependency = WorkspaceTasks(workspace).create( title="Completed dependency")
    dependency.status = TaskStatus.DONE
    dependency.pipeline_status = PipelineStatus.DONE
    WorkspaceTasks(workspace).save(dependency)
    dependent = WorkspaceTasks(workspace).create( title="Depends on completed task", depends_on=[dependency.id])

    selection = TaskQueueService(workspace).peek_next_selection()

    assert selection.task is not None
    assert selection.task.id == dependent.id
    assert selection.blocked == []


@pytest.mark.parametrize("flag_reason", ["rejection_loop_detected", "semantic_reject"])
def test_dequeue_skips_flagged_manual_intervention_tasks(tmp_path: Path, flag_reason: str) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    blocked = WorkspaceTasks(workspace).create( title="Needs manual review")
    runnable = WorkspaceTasks(workspace).create( title="Runnable next task")

    blocked_task = WorkspaceTasks(workspace).require(blocked.id)
    blocked_task.status = TaskStatus.FLAGGED
    blocked_task.pipeline_status = PipelineStatus.FLAGGED
    blocked_task.flag_reason = flag_reason
    WorkspaceTasks(workspace).save(blocked_task)

    state = WorkspaceStateRepository(workspace).load()
    state.queue = [blocked.id, runnable.id]
    WorkspaceStateRepository(workspace).save(state)

    selection = TaskQueueService(workspace).select_next()

    assert selection.task is not None
    assert selection.task.id == runnable.id

    repaired_state = WorkspaceStateRepository(workspace).load()
    assert repaired_state.active_task_id == runnable.id
    assert blocked.id in repaired_state.queue
