import json
import sqlite3
from pathlib import Path

import pytest

from litehive.config.paths import workspace_path
from litehive.config.workspace import create_workspace
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.tasks.queue import (
    TaskQueueService,
)
from litehive.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    create_workspace(tmp_path)
    return Workspace.from_path(tmp_path)


def _latest_audit_context(tmp_path: Path, task_id: str, action: str) -> dict:
    with sqlite3.connect(workspace_path(tmp_path, "data.db")) as connection:
        row = connection.execute(
            """
            SELECT context_json
            FROM task_audit_log
            WHERE task_id = ? AND action = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (task_id, action),
        ).fetchone()

    assert row is not None
    return json.loads(row[0])


def test_enqueue_task_repositions_without_duplicates_and_audits_front_flag(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = WorkspaceTasks(workspace).create( title="First task")
    second = WorkspaceTasks(workspace).create( title="Second task")

    state = TaskQueueService(workspace).enqueue(second.id, front=True)

    assert state.queue == [second.id, first.id]
    assert _latest_audit_context(tmp_path, second.id, "queue_enqueued") == {"front": True}

    state = TaskQueueService(workspace).enqueue(second.id)

    assert state.queue == [first.id, second.id]
    assert _latest_audit_context(tmp_path, second.id, "queue_enqueued") == {"front": False}


def test_move_queued_task_uses_one_based_positions_and_clamps_to_queue_end(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = WorkspaceTasks(workspace).create( title="First task")
    second = WorkspaceTasks(workspace).create( title="Second task")
    third = WorkspaceTasks(workspace).create( title="Third task")

    state = TaskQueueService(workspace).move(third.id, 1)

    assert state.queue == [third.id, first.id, second.id]
    assert _latest_audit_context(tmp_path, third.id, "queue_moved") == {"requested_position": 1}

    state = TaskQueueService(workspace).move(third.id, 99)

    assert state.queue == [first.id, second.id, third.id]
    assert _latest_audit_context(tmp_path, third.id, "queue_moved") == {"requested_position": 99}


def test_prioritize_queued_tasks_preserves_requested_order_then_remaining_order(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = WorkspaceTasks(workspace).create( title="First task")
    second = WorkspaceTasks(workspace).create( title="Second task")
    third = WorkspaceTasks(workspace).create( title="Third task")
    fourth = WorkspaceTasks(workspace).create( title="Fourth task")

    state = TaskQueueService(workspace).prioritize([third.id, first.id])

    assert state.queue == [third.id, first.id, second.id, fourth.id]
    assert _latest_audit_context(tmp_path, third.id, "queue_prioritized") == {
        "requested_order": [third.id, first.id]
    }
    assert _latest_audit_context(tmp_path, first.id, "queue_prioritized") == {
        "requested_order": [third.id, first.id]
    }


def test_queue_mutations_reject_active_task_conflicts_without_reordering(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = WorkspaceTasks(workspace).create( title="Active queued task")
    second = WorkspaceTasks(workspace).create( title="Second task")

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = first.id
    WorkspaceStateRepository(workspace).save(state)

    with pytest.raises(WorkspaceConflictError, match="runner is actively using task state"):
        TaskQueueService(workspace).move(first.id, 2)

    refreshed_state = WorkspaceStateRepository(workspace).load()
    assert refreshed_state.active_task_id == first.id
    assert refreshed_state.queue == [first.id, second.id]
