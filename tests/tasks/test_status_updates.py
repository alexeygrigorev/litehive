from collections.abc import Callable
from pathlib import Path
import inspect
import json

import pytest

from litehive.config.workspace import create_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.task import TaskRecord
from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound
from litehive.workspace import Workspace
from litehive.state.persist import load_state_for_workspace
from litehive.state.records import (
    create_task_for_workspace,
    require_task_for_workspace,
    save_task_for_workspace,
)
from litehive.state.store import runtime_store_for_workspace
from litehive.tasks.status import (
    close_task_for_workspace,
    park_task_for_workspace,
    update_task_for_workspace,
)
from litehive.domain.common import PipelineState, PipelineStatus, TaskStatus


def _raw_task_state_payload(root: Path, task_id: str) -> dict:
    with connect_workspace_db(root) as connection:
        row = connection.execute("SELECT payload FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return json.loads(row["payload"])


def _save_intent_only_task(workspace: Workspace, task_id: str = "T-0001", *, goal: str = "") -> None:
    runtime_store_for_workspace(workspace).save_task_intent(
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
    assert "engine" not in inspect.signature(update_task_for_workspace).parameters


def test_update_task_rejects_removed_engine_kwarg(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="No engine override")

    # Hide the kwarg behind a callable indirection so the static type
    # checker cannot see the removed `engine` keyword and still has a
    # plain Callable to verify; the test asserts the runtime TypeError.
    callable_update_task: Callable[..., object] = update_task_for_workspace
    with pytest.raises(TypeError, match="engine"):
        callable_update_task(workspace, task.id, engine="gemini")


def test_update_task_closes_task_with_structured_outcome(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Close me")
    persistence = SqlitePersistence(workspace)
    state = persistence.initialize(task.id)
    state.stage = PipelineState.RECOVERING
    persistence.save(state)

    update_task_for_workspace(
        workspace,
        task.id,
        outcome="wont_do",
        outcome_reason="not worth it",
    )

    refreshed = require_task_for_workspace(workspace, task.id)
    state = load_state_for_workspace(workspace)

    assert refreshed.status == "closed"
    assert refreshed.close_reason == "wont_do"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.pipeline.execution_status == "cancelled"
    assert refreshed.runtime.pipeline.last_outcome.reason_code == "task_closed"
    assert refreshed.runtime.pipeline.last_outcome.kind == "closed"
    assert refreshed.runtime.pipeline.last_outcome.reason == "not worth it"
    assert state.active_task_id is None
    assert task.id not in state.queue
    raw_state = _raw_task_state_payload(tmp_path, task.id)
    assert raw_state["status"] == "closed"
    assert raw_state["close_reason"] == "wont_do"
    assert raw_state["runtime"]["pipeline"]["last_outcome"]["kind"] == "closed"
    assert raw_state["runtime"]["pipeline"]["last_outcome"]["reason_code"] == "task_closed"
    with pytest.raises(TaskNotFound):
        persistence.load(task.id)


def test_update_task_parks_task_with_structured_action(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Park me")

    update_task_for_workspace(workspace, task.id, action="park")

    refreshed = require_task_for_workspace(workspace, task.id)
    state = load_state_for_workspace(workspace)

    assert refreshed.status == "parked"
    assert refreshed.runtime.pipeline.execution_status == "paused"
    assert refreshed.runtime.execution.active_subagent is None
    assert state.active_task_id is None
    assert task.id not in state.queue


def test_update_task_requeues_task_with_structured_action(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Retry me")
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task_for_workspace(workspace, task)

    persistence = SqlitePersistence(workspace)
    failed_state = persistence.initialize(task.id)
    failed_state.stage = PipelineState.FAILED
    persistence.save(failed_state)

    update_task_for_workspace(workspace, task.id, action="requeue")

    refreshed = require_task_for_workspace(workspace, task.id)
    state = load_state_for_workspace(workspace)

    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert state.queue[-1] == task.id
    with pytest.raises(TaskNotFound):
        persistence.load(task.id)


def test_update_task_abandons_task_with_structured_action(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Stop me")
    persistence = SqlitePersistence(workspace)
    state = persistence.initialize(task.id)
    state.stage = PipelineState.TESTING
    persistence.save(state)
    task.status = TaskStatus.PARKED
    task.pipeline_status = PipelineStatus.TESTING
    save_task_for_workspace(workspace, task)

    update_task_for_workspace(workspace, task.id, action="abandon")

    refreshed = require_task_for_workspace(workspace, task.id)
    state = load_state_for_workspace(workspace)

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
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Target task")

    _save_intent_only_task(workspace, "T-0002")

    update_task_for_workspace(workspace, task.id, goal="Updated safely")

    refreshed = require_task_for_workspace(workspace, task.id)
    assert refreshed.goal == "Updated safely"


def test_update_task_tolerates_missing_runtime_row_on_target_task(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    _save_intent_only_task(workspace, goal="Original goal")

    update_task_for_workspace(workspace, "T-0001", goal="Updated safely")

    refreshed = require_task_for_workspace(workspace, "T-0001")
    assert refreshed.goal == "Updated safely"


def test_update_task_accepts_injected_workspace(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Update through workspace")

    updated = update_task_for_workspace(workspace, task.id, goal="Updated safely")

    assert updated.goal == "Updated safely"
    assert require_task_for_workspace(workspace, task.id).goal == "Updated safely"


def test_close_task_tolerates_missing_runtime_row_on_target_task(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    _save_intent_only_task(workspace)

    close_task_for_workspace(
        workspace,
        "T-0001",
        outcome="duplicate",
        reason="duplicate umbrella",
    )

    refreshed = require_task_for_workspace(workspace, "T-0001")
    assert refreshed.status == "closed"
    assert refreshed.close_reason == "duplicate"
    assert refreshed.runtime.pipeline.last_outcome.reason == "duplicate umbrella"
    raw_state = _raw_task_state_payload(tmp_path, "T-0001")
    assert raw_state["status"] == "closed"
    assert raw_state["close_reason"] == "duplicate"
    assert raw_state["runtime"]["pipeline"]["last_outcome"]["kind"] == "closed"
    assert raw_state["runtime"]["pipeline"]["last_outcome"]["reason_code"] == "task_closed"


def test_close_task_resets_pipeline_state_row(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Close and clear pipeline state")
    persistence = SqlitePersistence(workspace)
    state = persistence.initialize(task.id)
    state.stage = PipelineState.RECOVERING
    persistence.save(state)

    close_task_for_workspace(
        workspace,
        task.id,
        outcome="duplicate",
        reason="duplicate umbrella",
    )

    refreshed = require_task_for_workspace(workspace, task.id)
    assert refreshed.status == "closed"
    assert refreshed.close_reason == "duplicate"
    assert refreshed.runtime.pipeline.last_outcome.reason == "duplicate umbrella"
    with pytest.raises(TaskNotFound):
        persistence.load(task.id)


def test_close_and_park_accept_injected_workspace(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    close_me = create_task_for_workspace(workspace, title="Close through workspace")
    park_me = create_task_for_workspace(workspace, title="Park through workspace")

    closed = close_task_for_workspace(workspace, close_me.id, outcome="duplicate", reason="same work")
    parked = park_task_for_workspace(workspace, park_me.id)

    assert closed.status == "closed"
    assert closed.close_reason == "duplicate"
    assert closed.runtime.pipeline.last_outcome.reason == "same work"
    assert parked.status == "parked"
    assert parked.runtime.pipeline.execution_status == "paused"
