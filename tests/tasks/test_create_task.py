import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import litehive.state.records as tasks_crud
from litehive.cli.task_cli import app as task_app
from litehive.config.workspace import create_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.reports import FollowUpTaskSpec
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.tasks.status import TaskStatusService
from litehive.workspace import Workspace


def _task_intent_payload(root: Path, task_id: str) -> dict:
    with connect_workspace_db(root) as connection:
        row = connection.execute("SELECT payload FROM task_intent WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return json.loads(row["payload"])


def _help_option_names(output: str) -> set[str]:
    return set(re.findall(r"(?<![\w-])--[\w-]+", output))


def test_create_task_persists_folder_and_queue(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    task = WorkspaceTasks(workspace).create( title="Fix login race")
    tasks = WorkspaceTasks(workspace).list()
    state = WorkspaceStateRepository(workspace).load()

    assert task.id == "T-0001"
    assert len(tasks) == 1
    assert state.queue == ["T-0001"]
    task_path = tmp_path / ".litehive" / "tasks" / "T-0001-fix-login-race"
    assert task_path.exists()
    assert not (task_path / "task.yaml").exists()
    assert _task_intent_payload(tmp_path, task.id)["title"] == "Fix login race"


def test_create_task_persists_manual_creation_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    monkeypatch.delenv("LITEHIVE_STAGE", raising=False)
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    task = WorkspaceTasks(workspace).create( title="Manual provenance")
    persisted = WorkspaceTasks(workspace).get(task.id)
    data = _task_intent_payload(tmp_path, task.id)

    assert persisted is not None
    assert persisted.created_from is not None
    assert persisted.created_from.source == "manual"
    assert persisted.created_from.task_id is None
    assert persisted.created_from.role is None
    assert persisted.created_from.rationale == "Created outside a Litehive agent session."
    assert data["created_from"]["source"] == "manual"
    assert data["created_from"]["task_id"] is None
    assert data["created_from"]["role"] is None


def test_create_task_defaults_to_full_pipeline_mode(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    task = WorkspaceTasks(workspace).create( title="Default pipeline mode")
    persisted = WorkspaceTasks(workspace).get(task.id)

    assert persisted is not None
    assert persisted.pipeline_mode == "full"


def test_create_task_persists_single_pipeline_mode(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    task = WorkspaceTasks(workspace).create( title="Single pipeline mode", pipeline_mode="single")
    persisted = WorkspaceTasks(workspace).get(task.id)

    assert persisted is not None
    assert persisted.pipeline_mode == "single"


def test_create_task_persists_agent_creation_provenance_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    monkeypatch.delenv("LITEHIVE_STAGE", raising=False)
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    parent = WorkspaceTasks(workspace).create( title="Current agent task")
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setenv("LITEHIVE_TASK_ID", parent.id)
    monkeypatch.setenv("LITEHIVE_STAGE", "implementing")

    task = WorkspaceTasks(workspace).create( title="Created from agent session")
    persisted = WorkspaceTasks(workspace).get(task.id)

    assert persisted is not None
    assert persisted.created_from is not None
    assert persisted.created_from.source == "agent"
    assert persisted.created_from.task_id == parent.id
    assert persisted.created_from.stage == "implementing"
    assert persisted.created_from.role == "swe"
    assert persisted.created_from.blocking is False
    assert persisted.created_from.rationale == f"Created by Litehive agent role swe while working on {parent.id}."


def test_task_add_cli_persists_surviving_flags(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    first = WorkspaceTasks(workspace).create( title="First prerequisite")
    second = WorkspaceTasks(workspace).create( title="Second prerequisite")

    result = CliRunner().invoke(
        task_app,
        [
            "add",
            "Queued task",
            "--workspace",
            str(tmp_path),
            "--goal",
            "Complete the queued task",
            "--acceptance-criteria",
            "Task ships cleanly",
            "--depends-on",
            f"{first.id},{second.id}",
            "--priority",
            "high",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "priority: high" in result.output
    assert f"depends_on: {first.id}, {second.id}" in result.output

    persisted = WorkspaceTasks(workspace).get("T-0003")
    assert persisted is not None
    assert persisted.pipeline_mode == "full"
    assert persisted.goal == "Complete the queued task"
    assert persisted.acceptance_criteria == ["Task ships cleanly"]
    assert persisted.depends_on == [first.id, second.id]
    assert persisted.priority == "high"


def test_task_add_help_matches_trimmed_option_surface() -> None:
    result = CliRunner().invoke(task_app, ["add", "--help"], standalone_mode=False)

    assert result.exit_code == 0, result.output
    option_names = _help_option_names(result.output)
    for option in [
        "--goal",
        "--acceptance-criteria",
        "--depends-on",
        "--priority",
    ]:
        assert option in option_names
    for option in [
        "--engine",
        "--model",
        "--retry-limit",
        "--record-mode",
        "--task-type",
        "--mode",
        "--pipeline-mode",
        "--pm-complexity",
        "--planned-effort",
        "--auto-commit",
        "--human-checkpoints",
    ]:
        assert option not in option_names


def test_task_add_cli_defaults_to_full_pipeline_mode(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    result = CliRunner().invoke(
        task_app,
        [
            "add",
            "Full mode default",
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "pipeline_mode: full" in result.output

    persisted = WorkspaceTasks(workspace).get("T-0001")
    assert persisted is not None
    assert persisted.pipeline_mode == "full"


@pytest.mark.parametrize("command", ["browse", "recent", "search"])
def test_task_discovery_convenience_cli_is_removed(command: str) -> None:
    result = CliRunner().invoke(task_app, [command, "dashboard"], standalone_mode=False)

    assert result.exit_code != 0
    diagnostic = result.output or str(result.exception)
    assert f"No such command '{command}'" in diagnostic


def test_task_update_help_matches_trimmed_option_surface() -> None:
    result = CliRunner().invoke(task_app, ["update", "--help"], standalone_mode=False)

    assert result.exit_code == 0, result.output
    option_names = _help_option_names(result.output)
    for option in [
        "--title",
        "--priority",
        "--goal",
        "--depends-on",
        "--acceptance-criteria",
        "--constraint",
        "--plan-step",
    ]:
        assert option in option_names
    for option in [
        "--engine",
        "--model",
        "--retry-limit",
        "--task-type",
        "--pm-complexity",
        "--planned-effort",
        "--auto-commit",
        "--human-checkpoints",
        "--from-file",
        "--edit",
    ]:
        assert option not in option_names


def test_task_update_cli_persists_surviving_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    WorkspaceTasks(workspace).create( title="First prerequisite")
    second = WorkspaceTasks(workspace).create( title="Second prerequisite")
    task = WorkspaceTasks(workspace).create( title="Original title")

    result = CliRunner().invoke(
        task_app,
        [
            "update",
            task.id,
            "--workspace",
            str(tmp_path),
            "--title",
            "Updated title",
            "--priority",
            "high",
            "--goal",
            "Updated goal",
            "--depends-on",
            second.id,
            "--acceptance-criteria",
            "Updated criterion",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert f"task: {task.id} Updated title" in result.output
    assert "priority: high" in result.output
    assert f"depends_on: {second.id}" in result.output

    persisted = WorkspaceTasks(workspace).get(task.id)
    assert persisted is not None
    assert persisted.title == "Updated title"
    assert persisted.priority == "high"
    assert persisted.goal == "Updated goal"
    assert persisted.depends_on == [second.id]
    assert persisted.acceptance_criteria == ["Updated criterion"]
    assert persisted.constraints == []
    assert persisted.plan == []


def test_create_task_preserves_runner_queue_changes_after_state_snapshot(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    script = """
import json
from pathlib import Path
from litehive.config.workspace import create_workspace
from litehive.state.records import WorkspaceTasks
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.persist import WorkspaceStateRepository
from litehive.workspace import Workspace
import litehive.state.persist

root = Path(__import__("sys").argv[1])
create_workspace(root)
workspace = Workspace.from_path(root)
first = WorkspaceTasks(workspace).create( title="First queued task")
second = WorkspaceTasks(workspace).create( title="Second queued task")
original_merge = litehive.state.persist.WorkspaceStateRepository.merged_state_for_runner_owned_write
injected = False

def inject_latest_state(self, *, state, protected_task_ids=()):
    global injected
    if not injected:
        injected = True
        workspace = self.workspace
        latest = WorkspaceStateRepository(workspace).load()
        latest.queue = [second.id, first.id]
        WorkspaceStateRepository(workspace).save_without_runner_guard(latest)
    return original_merge(self, state=state, protected_task_ids=protected_task_ids)

litehive.state.persist.WorkspaceStateRepository.merged_state_for_runner_owned_write = inject_latest_state
added = WorkspaceTasks(workspace).create( title="Added while runner updated queue")
print(json.dumps({"id": added.id, "queue": WorkspaceStateRepository(workspace).load().queue}))
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
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    WorkspaceTasks(workspace).create( title="Existing task")
    WorkspaceTasks(workspace).create( title="Second task")

    state = WorkspaceStateRepository(workspace).load()
    state.next_task_number = 0
    WorkspaceStateRepository(workspace).save(state)

    created = WorkspaceTasks(workspace).create( title="Third task")

    assert created.id == "T-0003"
    assert WorkspaceStateRepository(workspace).load().next_task_number == 3


def test_create_task_uses_persisted_next_task_number_without_rescanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    WorkspaceTasks(workspace).create( title="Existing task")

    def fail_scan(workspace: Workspace) -> int:
        raise AssertionError("task id allocation should not rescan task directories")

    monkeypatch.setattr(tasks_crud, "_highest_task_number_in_store_impl", fail_scan)

    created = WorkspaceTasks(workspace).create( title="Second task")

    assert created.id == "T-0002"
    assert WorkspaceStateRepository(workspace).load().next_task_number == 2


def test_create_task_persists_dependencies(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    first = WorkspaceTasks(workspace).create( title="First prerequisite")
    second = WorkspaceTasks(workspace).create( title="Second prerequisite")
    dependent = WorkspaceTasks(workspace).create(
        title="Dependent task",
        depends_on=[first.id, second.id],
    )

    persisted = WorkspaceTasks(workspace).get(dependent.id)

    assert persisted is not None
    assert persisted.depends_on == [first.id, second.id]


def test_create_task_rejects_missing_dependency(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    with pytest.raises(ValueError, match="Task T-9999 not found"):
        WorkspaceTasks(workspace).create( title="Dependent task", depends_on=["T-9999"])


def test_create_task_rejects_dependency_cycle(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    first = WorkspaceTasks(workspace).create( title="First task")
    second = WorkspaceTasks(workspace).create( title="Second task")
    first.depends_on = [second.id]
    WorkspaceTasks(workspace).save(first)

    with pytest.raises(ValueError, match=rf"Task {second.id} dependency cycle detected via {first.id}"):
        TaskStatusService(workspace).update(second.id, depends_on=[first.id])


def test_create_follow_up_tasks_persists_queue_and_creation_source(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    parent = WorkspaceTasks(workspace).create( title="Parent task")

    created = WorkspaceTasks(workspace).create_follow_ups(
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
    follow_up = WorkspaceTasks(workspace).get(created[0].id)
    assert follow_up is not None
    assert follow_up.created_from is not None
    assert follow_up.created_from.source == "follow_up"
    assert follow_up.created_from.task_id == parent.id
    assert follow_up.created_from.stage == "accepting"
    assert follow_up.created_from.blocking is True
    assert WorkspaceStateRepository(workspace).load().queue[-1] == created[0].id
