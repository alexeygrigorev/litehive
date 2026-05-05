from pathlib import Path
import inspect
import json

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.task import TaskRecord
from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound
from litehive.workspace import Workspace
from litehive.state.persist import load_state
from litehive.state.records import create_task, require_task, save_task
from litehive.state.store import runtime_store
from litehive.tasks.status import close_task, update_task
from litehive.domain.common import PipelineState, PipelineStatus, TaskStatus


def _raw_task_state_payload(root: Path, task_id: str) -> dict:
    with connect_workspace_db(root) as connection:
        row = connection.execute("SELECT payload FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return json.loads(row["payload"])


def _save_intent_only_task(root: Path, task_id: str = "T-0001", *, goal: str = "") -> None:
    runtime_store(root).save_task_intent(
        task_id,
        TaskRecord(
            id=task_id,
            slug="missing-runtime",
            title="Missing runtime row",
            pipeline_mode="full",
            priority="medium",
            goal=goal,
            git={
                "auto_commit": True,
                "commit_message": "missing runtime row",
            },
        ).to_intent_record(),
    )


def test_update_task_signature_excludes_removed_engine_kwarg() -> None:
    assert "engine" not in inspect.signature(update_task).parameters


def test_update_task_rejects_removed_engine_kwarg(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="No engine override")

    with pytest.raises(TypeError, match="engine"):
        update_task(tmp_path, task.id, **{"engine": "gemini"})


def test_update_task_closes_task_with_structured_outcome(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close me")
    persistence = SqlitePersistence(Workspace.from_path(tmp_path))
    state = persistence.initialize(task.id)
    state.stage = PipelineState.RECOVERING
    persistence.save(state)

    update_task(
        tmp_path,
        task.id,
        outcome="wont_do",
        outcome_reason="not worth it",
    )

    refreshed = require_task(tmp_path, task.id)
    state = load_state(tmp_path)

    assert refreshed.status == "closed"
    assert refreshed.close_reason == "wont_do"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.pipeline.execution_status == "cancelled"
    assert refreshed.runtime.pipeline.last_outcome.reason_code == "wont_do"
    assert refreshed.runtime.pipeline.last_outcome.kind == "closed"
    assert refreshed.runtime.pipeline.last_outcome.reason == "not worth it"
    assert state.active_task_id is None
    assert task.id not in state.queue
    raw_state = _raw_task_state_payload(tmp_path, task.id)
    assert raw_state["status"] == "closed"
    assert raw_state["close_reason"] == "wont_do"
    assert raw_state["runtime"]["pipeline"]["last_outcome"]["kind"] == "closed"
    assert raw_state["runtime"]["pipeline"]["last_outcome"]["reason_code"] == "wont_do"
    with pytest.raises(TaskNotFound):
        persistence.load(task.id)


def test_update_task_parks_task_with_structured_action(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Park me")

    update_task(tmp_path, task.id, action="park")

    refreshed = require_task(tmp_path, task.id)
    state = load_state(tmp_path)

    assert refreshed.status == "parked"
    assert refreshed.runtime.pipeline.execution_status == "paused"
    assert refreshed.runtime.execution.active_subagent is None
    assert state.active_task_id is None
    assert task.id not in state.queue


def test_update_task_requeues_task_with_structured_action(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Retry me")
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task(tmp_path, task)

    persistence = SqlitePersistence(Workspace.from_path(tmp_path))
    failed_state = persistence.initialize(task.id)
    failed_state.stage = PipelineState.FAILED
    persistence.save(failed_state)

    update_task(tmp_path, task.id, action="requeue")

    refreshed = require_task(tmp_path, task.id)
    state = load_state(tmp_path)

    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert state.queue[-1] == task.id
    with pytest.raises(TaskNotFound):
        persistence.load(task.id)


def test_update_task_abandons_task_with_structured_action(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Stop me")
    persistence = SqlitePersistence(Workspace.from_path(tmp_path))
    state = persistence.initialize(task.id)
    state.stage = PipelineState.TESTING
    persistence.save(state)
    task.status = TaskStatus.PARKED
    task.pipeline_status = PipelineStatus.TESTING
    save_task(tmp_path, task)

    update_task(tmp_path, task.id, action="abandon")

    refreshed = require_task(tmp_path, task.id)
    state = load_state(tmp_path)

    assert refreshed.status == "closed"
    assert refreshed.close_reason == "execution_cancelled"
    assert refreshed.runtime.pipeline.execution_status == "cancelled"
    assert refreshed.runtime.pipeline.last_outcome.reason_code == "execution_cancelled"
    assert refreshed.runtime.pipeline.last_outcome.kind == "closed"
    assert refreshed.runtime.pipeline.last_outcome.reason == "Task abandoned via structured report."
    assert state.active_task_id is None
    assert task.id not in state.queue
    with pytest.raises(TaskNotFound):
        persistence.load(task.id)


def test_update_task_ignores_unrelated_missing_runtime_rows(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Target task")

    _save_intent_only_task(tmp_path, "T-0002")

    update_task(tmp_path, task.id, goal="Updated safely")

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.goal == "Updated safely"


def test_update_task_tolerates_missing_runtime_row_on_target_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _save_intent_only_task(tmp_path, goal="Original goal")

    update_task(tmp_path, "T-0001", goal="Updated safely")

    refreshed = require_task(tmp_path, "T-0001")
    assert refreshed.goal == "Updated safely"


def test_close_task_tolerates_missing_runtime_row_on_target_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _save_intent_only_task(tmp_path)

    close_task(tmp_path, "T-0001", outcome="duplicate", reason="duplicate umbrella")

    refreshed = require_task(tmp_path, "T-0001")
    assert refreshed.status == "closed"
    assert refreshed.close_reason == "duplicate"
    assert refreshed.runtime.pipeline.last_outcome.reason == "duplicate umbrella"
    raw_state = _raw_task_state_payload(tmp_path, "T-0001")
    assert raw_state["status"] == "closed"
    assert raw_state["close_reason"] == "duplicate"
    assert raw_state["runtime"]["pipeline"]["last_outcome"]["kind"] == "closed"
    assert raw_state["runtime"]["pipeline"]["last_outcome"]["reason_code"] == "duplicate"


def test_close_task_resets_pipeline_state_row(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close and clear pipeline state")
    persistence = SqlitePersistence(Workspace.from_path(tmp_path))
    state = persistence.initialize(task.id)
    state.stage = PipelineState.RECOVERING
    persistence.save(state)

    close_task(tmp_path, task.id, outcome="duplicate", reason="duplicate umbrella")

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.status == "closed"
    assert refreshed.close_reason == "duplicate"
    assert refreshed.runtime.pipeline.last_outcome.reason == "duplicate umbrella"
    with pytest.raises(TaskNotFound):
        persistence.load(task.id)
