from pathlib import Path

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, require_task, save_task
from litehive.tasks.queue import dequeue_next_task_selection, peek_next_task_selection


def _persist_task_status(root: Path, task_id: str, *, status: str, pipeline_status: str) -> None:
    task = require_task(root, task_id)
    task.status = status
    task.pipeline_status = pipeline_status
    if status == "interrupted":
        task.runtime.execution_status = "interrupted"
    save_task(root, task)



def test_dequeue_next_task_reclaims_missing_in_progress_task_before_handoff(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    unfinished = create_task(tmp_path, title="Unfinished active task")
    later = create_task(tmp_path, title="Later queued task")
    _persist_task_status(
        tmp_path,
        unfinished.id,
        status="in_progress",
        pipeline_status="implementing",
    )
    later_task = require_task(tmp_path, later.id)
    later_task.priority = "high"
    save_task(tmp_path, later_task)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [later.id]
    save_state(tmp_path, state)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == unfinished.id

    repaired_state = load_state(tmp_path)
    assert repaired_state.active_task_id == unfinished.id
    assert repaired_state.queue == [later.id]


def test_dequeue_next_task_ignores_stale_active_marker_when_reclaiming_missing_work(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    stale = create_task(tmp_path, title="Stale active task")
    unfinished = create_task(tmp_path, title="Unfinished active task")
    later = create_task(tmp_path, title="Later queued task")
    _persist_task_status(
        tmp_path,
        stale.id,
        status="done",
        pipeline_status="done",
    )
    _persist_task_status(
        tmp_path,
        unfinished.id,
        status="in_progress",
        pipeline_status="implementing",
    )

    state = load_state(tmp_path)
    state.active_task_id = stale.id
    state.queue = [later.id]
    save_state(tmp_path, state)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == unfinished.id

    repaired_state = load_state(tmp_path)
    assert repaired_state.active_task_id == unfinished.id
    assert repaired_state.queue == [later.id]


@pytest.mark.parametrize(
    ("status", "pipeline_status"),
    [
        ("queued", "implementing"),
    ],
    ids=["resumed"],
)
def test_dequeue_next_task_preserves_missing_resumed_work_on_restart(
    tmp_path: Path,
    status: str,
    pipeline_status: str,
) -> None:
    ensure_workspace(tmp_path)
    unfinished = create_task(tmp_path, title="Unfinished task")
    later = create_task(tmp_path, title="Later queued task")
    _persist_task_status(
        tmp_path,
        unfinished.id,
        status=status,
        pipeline_status=pipeline_status,
    )
    queued_task = require_task(tmp_path, unfinished.id)
    queued_task.runtime.current_stage.stage = pipeline_status
    queued_task.runtime.current_stage.status = "idle"
    save_task(tmp_path, queued_task)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [later.id]
    save_state(tmp_path, state)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None

    repaired_state = load_state(tmp_path)
    visible_task_ids = {repaired_state.active_task_id, *repaired_state.queue}
    assert unfinished.id in visible_task_ids


@pytest.mark.parametrize(
    ("status", "pipeline_status"),
    [
        ("queued", "implementing"),
    ],
    ids=["resumed"],
)
def test_peek_next_task_restores_missing_unfinished_tasks_to_queue_on_restart(
    tmp_path: Path,
    status: str,
    pipeline_status: str,
) -> None:
    ensure_workspace(tmp_path)
    unfinished = create_task(tmp_path, title="Unfinished task")
    later = create_task(tmp_path, title="Later queued task")
    _persist_task_status(
        tmp_path,
        unfinished.id,
        status=status,
        pipeline_status=pipeline_status,
    )
    queued_task = require_task(tmp_path, unfinished.id)
    queued_task.runtime.current_stage.stage = pipeline_status
    queued_task.runtime.current_stage.status = "idle"
    save_task(tmp_path, queued_task)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [later.id]
    save_state(tmp_path, state)

    selection = peek_next_task_selection(tmp_path)

    assert selection.task is not None

    repaired_state = load_state(tmp_path)
    assert repaired_state.active_task_id is None
    assert repaired_state.queue == [later.id, unfinished.id]


@pytest.mark.parametrize("flag_reason", ["rejection_loop_detected", "semantic_reject"])
def test_dequeue_skips_flagged_manual_intervention_tasks(tmp_path: Path, flag_reason: str) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Needs manual review")
    runnable = create_task(tmp_path, title="Runnable next task")

    blocked_task = require_task(tmp_path, blocked.id)
    blocked_task.status = "flagged"
    blocked_task.pipeline_status = "flagged"
    blocked_task.flag_reason = flag_reason
    save_task(tmp_path, blocked_task)

    state = load_state(tmp_path)
    state.queue = [blocked.id, runnable.id]
    save_state(tmp_path, state)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == runnable.id

    repaired_state = load_state(tmp_path)
    assert repaired_state.active_task_id == runnable.id
    assert blocked.id in repaired_state.queue
