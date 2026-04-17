import os
import subprocess
import sys
from pathlib import Path

import pytest

import litehive.state.records as tasks_crud
from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import FollowUpTaskSpec
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_follow_up_tasks, create_task, get_task, list_tasks, save_task
from litehive.tasks.status import update_task_metadata


def test_create_task_persists_folder_and_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Fix login race")
    tasks = list_tasks(tmp_path)
    state = load_state(tmp_path)

    assert task.id == "T-0001"
    assert len(tasks) == 1
    assert state.queue == ["T-0001"]
    assert (tmp_path / ".litehive" / "tasks" / "T-0001-fix-login-race" / "task.yaml").exists()


def test_create_task_preserves_runner_queue_changes_after_state_snapshot(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    script = """
import json
from pathlib import Path
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task
from litehive.state.persist import load_state
from litehive.state.persist import save_state_without_runner_guard
from litehive.state import persist as workflow_module

root = Path(__import__("sys").argv[1])
ensure_workspace(root)
first = create_task(root, title="First queued task")
second = create_task(root, title="Second queued task")
original_merge = workflow_module.merged_state_for_runner_owned_write
injected = False

def inject_latest_state(root, *, state, protected_task_ids=()):
    global injected
    if not injected:
        injected = True
        latest = load_state(root)
        latest.queue = [second.id, first.id]
        save_state_without_runner_guard(root, latest)
    return original_merge(root, state=state, protected_task_ids=protected_task_ids)

workflow_module.merged_state_for_runner_owned_write = inject_latest_state
added = create_task(root, title="Added while runner updated queue")
print(json.dumps({"id": added.id, "queue": load_state(root).queue}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.stdout.strip() == '{"id": "T-0003", "queue": ["T-0002", "T-0001", "T-0003"]}'


def test_create_task_seeds_next_task_number_from_existing_workspace_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Existing task")
    create_task(tmp_path, title="Second task")

    state = load_state(tmp_path)
    state.next_task_number = 0
    save_state(tmp_path, state)

    created = create_task(tmp_path, title="Third task")

    assert created.id == "T-0003"
    assert load_state(tmp_path).next_task_number == 3


def test_create_task_uses_persisted_next_task_number_without_rescanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Existing task")

    def fail_scan(root: Path) -> int:
        raise AssertionError("task id allocation should not rescan task directories")

    monkeypatch.setattr(tasks_crud, "_highest_task_number_on_disk", fail_scan)

    created = create_task(tmp_path, title="Second task")

    assert created.id == "T-0002"
    assert load_state(tmp_path).next_task_number == 2


def test_create_task_persists_dependencies(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")
    dependent = create_task(
        tmp_path,
        title="Dependent task",
        depends_on=[first.id, second.id],
    )

    persisted = get_task(tmp_path, dependent.id)

    assert persisted is not None
    assert persisted.depends_on == [first.id, second.id]


def test_create_task_rejects_missing_dependency(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with pytest.raises(ValueError, match="Task T-9999 not found"):
        create_task(tmp_path, title="Dependent task", depends_on=["T-9999"])


def test_create_task_rejects_dependency_cycle(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    first.depends_on = [second.id]
    save_task(tmp_path, first)

    with pytest.raises(ValueError, match=rf"Task {second.id} dependency cycle detected via {first.id}"):
        update_task_metadata(tmp_path, second.id, depends_on=[first.id])


def test_create_follow_up_tasks_persists_queue_and_creation_source(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    parent = create_task(tmp_path, title="Parent task")

    created = create_follow_up_tasks(
        tmp_path,
        parent_task=parent,
        stage="accepting",
        follow_ups=[
            FollowUpTaskSpec(
                title="Document edge case",
                rationale="Need a follow-up after review",
                blocking=True,
            )
        ],
    )

    assert len(created) == 1
    follow_up = get_task(tmp_path, created[0].id)
    assert follow_up is not None
    assert follow_up.created_from is not None
    assert follow_up.created_from.task_id == parent.id
    assert follow_up.created_from.stage == "accepting"
    assert follow_up.created_from.blocking is True
    assert load_state(tmp_path).queue[-1] == created[0].id
