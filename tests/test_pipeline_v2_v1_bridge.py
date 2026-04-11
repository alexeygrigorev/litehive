"""Tests for the v1 TaskRecord ↔ v2 TaskState bridge."""

from pathlib import Path

import pytest

from litehive.pipeline.persistence import SqlitePersistence, TaskState
from litehive.pipeline.types import PipelineMode
from litehive.pipeline.v1_bridge import (
    TaskRecordNotFound,
    load_or_initialize,
    sync_back_to_task_record,
)
from litehive.tasks.crud import create_task, get_task

from tests.workspace_helpers import ensure_workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ensure_workspace(tmp_path)
    return tmp_path


def _add_test_task(workspace: Path, title: str, mode: str = "full") -> str:
    task = create_task(
        workspace,
        title=title,
        goal="test goal",
        pipeline_mode=mode,
    )
    return task.id


def test_load_or_initialize_creates_fresh_row(workspace: Path) -> None:
    task_id = _add_test_task(workspace, "bridge smoke")

    state = load_or_initialize(task_id, workspace)

    assert state.task_id == task_id
    assert state.stage == "ready"
    assert state.pipeline_mode == PipelineMode.FULL


def test_load_or_initialize_is_idempotent(workspace: Path) -> None:
    task_id = _add_test_task(workspace, "bridge idempotence")

    first = load_or_initialize(task_id, workspace)
    # Mutate the row through the runner path would normally do
    store = SqlitePersistence(workspace)
    first.stage = "implementing"
    first.stage_retry["implementing"] = 1
    store.save(first)

    second = load_or_initialize(task_id, workspace)
    assert second.stage == "implementing"
    assert second.stage_retry["implementing"] == 1


def test_load_or_initialize_picks_up_single_mode(workspace: Path) -> None:
    task_id = _add_test_task(workspace, "single mode task", mode="single")
    state = load_or_initialize(task_id, workspace)
    assert state.pipeline_mode == PipelineMode.SINGLE


def test_load_or_initialize_raises_for_unknown_task(workspace: Path) -> None:
    with pytest.raises(TaskRecordNotFound):
        load_or_initialize("T-9999", workspace)


def test_sync_back_done_updates_v1_task(workspace: Path) -> None:
    task_id = _add_test_task(workspace, "finished task")
    state = TaskState(task_id=task_id, stage="done", pipeline_mode=PipelineMode.FULL)

    updated = sync_back_to_task_record(state, workspace)
    assert updated is not None
    assert updated.status == "done"
    assert updated.pipeline_status == "done"

    # Verify it persisted
    reloaded = get_task(workspace, task_id)
    assert reloaded.status == "done"


def test_sync_back_failed_at_commit_stage_marks_merge_failed(workspace: Path) -> None:
    task_id = _add_test_task(workspace, "merge failed task")
    state = TaskState(
        task_id=task_id,
        stage="failed",
        pipeline_mode=PipelineMode.FULL,
        origin_stage="commit",
        failed_reason="recovery_exhausted",
    )

    updated = sync_back_to_task_record(state, workspace)
    assert updated.status == "merge_failed"
    assert updated.pipeline_status == "merge_failed"


def test_sync_back_failed_generic_is_flagged(workspace: Path) -> None:
    task_id = _add_test_task(workspace, "flagged task")
    state = TaskState(
        task_id=task_id,
        stage="failed",
        pipeline_mode=PipelineMode.FULL,
        origin_stage="implementing",
        failed_reason="recovery_exhausted",
    )

    updated = sync_back_to_task_record(state, workspace)
    assert updated.status == "flagged"


def test_sync_back_in_flight_updates_pipeline_status(workspace: Path) -> None:
    task_id = _add_test_task(workspace, "in flight task")
    state = TaskState(task_id=task_id, stage="implementing", pipeline_mode=PipelineMode.FULL)

    updated = sync_back_to_task_record(state, workspace)
    assert updated.status == "in_progress"
    assert updated.pipeline_status == "implementing"


def test_sync_back_returns_none_if_task_record_missing(workspace: Path) -> None:
    state = TaskState(task_id="T-DOES-NOT-EXIST", stage="done", pipeline_mode=PipelineMode.FULL)
    result = sync_back_to_task_record(state, workspace)
    assert result is None
