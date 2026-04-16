from pathlib import Path

import pytest
import yaml

from litehive.config.workspace import ensure_workspace
from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound
from litehive.state.persist import load_state
from litehive.state.records import create_task, require_task, save_task
from litehive.tasks.status import close_task, update_task


def test_update_task_closes_task_with_structured_outcome(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close me")

    update_task(
        tmp_path,
        task.id,
        outcome="wont_do",
        outcome_reason="not worth it",
    )

    refreshed = require_task(tmp_path, task.id)
    state = load_state(tmp_path)

    assert refreshed.status == "wont_do"
    assert refreshed.pipeline_status == "done"
    assert refreshed.runtime.execution_status == "cancelled"
    assert refreshed.runtime.last_outcome.reason_code == "wont_do"
    assert refreshed.runtime.last_outcome.reason == "not worth it"
    assert state.active_task_id is None
    assert task.id not in state.queue


def test_update_task_parks_task_with_structured_action(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Park me")

    update_task(tmp_path, task.id, action="park")

    refreshed = require_task(tmp_path, task.id)
    state = load_state(tmp_path)

    assert refreshed.status == "parked"
    assert refreshed.runtime.execution_status == "paused"
    assert refreshed.runtime.active_subagent is None
    assert state.active_task_id is None
    assert task.id not in state.queue


def test_update_task_requeues_task_with_structured_action(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Retry me")
    task.status = "flagged"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    persistence = SqlitePersistence(tmp_path)
    failed_state = persistence.initialize(task.id)
    failed_state.stage = "failed"
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
    task.status = "parked"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    update_task(tmp_path, task.id, action="abandon")

    refreshed = require_task(tmp_path, task.id)
    state = load_state(tmp_path)

    assert refreshed.status == "cancelled"
    assert refreshed.runtime.execution_status == "cancelled"
    assert refreshed.runtime.last_outcome.reason_code == "execution_cancelled"
    assert refreshed.runtime.last_outcome.reason == "Task abandoned via structured report."
    assert state.active_task_id is None
    assert task.id not in state.queue


def test_update_task_ignores_unrelated_missing_runtime_rows(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Target task")

    missing_dir = tmp_path / ".litehive" / "tasks" / "T-0002-missing-runtime"
    missing_dir.mkdir(parents=True)
    (missing_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "T-0002",
                "slug": "missing-runtime",
                "title": "Missing runtime row",
                "pipeline_mode": "full",
                "priority": "medium",
                "git": {
                    "auto_commit": True,
                    "commit_message": "missing runtime row",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    update_task(tmp_path, task.id, goal="Updated safely")

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.goal == "Updated safely"


def test_update_task_tolerates_missing_runtime_row_on_target_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task_dir = tmp_path / ".litehive" / "tasks" / "T-0001-missing-runtime"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "T-0001",
                "slug": "missing-runtime",
                "title": "Missing runtime row",
                "pipeline_mode": "full",
                "priority": "medium",
                "goal": "Original goal",
                "git": {
                    "auto_commit": True,
                    "commit_message": "missing runtime row",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    update_task(tmp_path, "T-0001", goal="Updated safely")

    refreshed = require_task(tmp_path, "T-0001")
    assert refreshed.goal == "Updated safely"


def test_close_task_tolerates_missing_runtime_row_on_target_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task_dir = tmp_path / ".litehive" / "tasks" / "T-0001-missing-runtime"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "T-0001",
                "slug": "missing-runtime",
                "title": "Missing runtime row",
                "pipeline_mode": "full",
                "priority": "medium",
                "git": {
                    "auto_commit": True,
                    "commit_message": "missing runtime row",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    close_task(tmp_path, "T-0001", outcome="duplicate", reason="duplicate umbrella")

    refreshed = require_task(tmp_path, "T-0001")
    assert refreshed.status == "duplicate"
    assert refreshed.runtime.last_outcome.reason == "duplicate umbrella"
