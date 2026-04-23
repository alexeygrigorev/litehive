import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner
import yaml

import litehive.state.records as tasks_crud
from litehive.cli.task_cli import app as task_app
from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import FollowUpTaskSpec
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_follow_up_tasks, create_task, get_task, list_tasks, save_task
from litehive.tasks.archive import archive_task
from litehive.tasks.duplicates import rebuild_duplicate_task_index, search_tasks_by_text
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


def test_create_task_persists_manual_creation_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    monkeypatch.delenv("LITEHIVE_STAGE", raising=False)
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Manual provenance")
    persisted = get_task(tmp_path, task.id)
    task_yaml = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))

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
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Default pipeline mode")
    persisted = get_task(tmp_path, task.id)

    assert persisted is not None
    assert persisted.pipeline_mode == "full"


def test_create_task_persists_single_pipeline_mode(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Single pipeline mode", pipeline_mode="single")
    persisted = get_task(tmp_path, task.id)

    assert persisted is not None
    assert persisted.pipeline_mode == "single"


def test_create_task_persists_agent_creation_provenance_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    monkeypatch.delenv("LITEHIVE_STAGE", raising=False)
    ensure_workspace(tmp_path)
    parent = create_task(tmp_path, title="Current agent task")
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setenv("LITEHIVE_TASK_ID", parent.id)
    monkeypatch.setenv("LITEHIVE_STAGE", "implementing")

    task = create_task(tmp_path, title="Created from agent session")
    persisted = get_task(tmp_path, task.id)

    assert persisted is not None
    assert persisted.created_from is not None
    assert persisted.created_from.source == "agent"
    assert persisted.created_from.task_id == parent.id
    assert persisted.created_from.stage == "implementing"
    assert persisted.created_from.role == "swe"
    assert persisted.created_from.blocking is False
    assert persisted.created_from.rationale == f"Created by Litehive agent role swe while working on {parent.id}."


def test_task_add_cli_persists_surviving_flags(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")

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

    persisted = get_task(tmp_path, "T-0003")
    assert persisted is not None
    assert persisted.pipeline_mode == "full"
    assert persisted.goal == "Complete the queued task"
    assert persisted.acceptance_criteria == ["Task ships cleanly"]
    assert persisted.depends_on == [first.id, second.id]
    assert persisted.priority == "high"


def test_task_add_help_matches_trimmed_option_surface() -> None:
    result = CliRunner().invoke(task_app, ["add", "--help"], standalone_mode=False)

    assert result.exit_code == 0, result.output
    for option in [
        "--goal",
        "--acceptance-criteria",
        "--depends-on",
        "--priority",
    ]:
        assert option in result.output
    for option in [
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
        assert option not in result.output


def test_task_add_cli_defaults_to_full_pipeline_mode(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

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

    persisted = get_task(tmp_path, "T-0001")
    assert persisted is not None
    assert persisted.pipeline_mode == "full"


def test_task_add_cli_warns_about_similar_tasks_in_supported_statuses(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    queued = create_task(
        tmp_path,
        title="Build web dashboard for task status",
        goal="Show queued tasks in the dashboard",
    )
    in_progress = create_task(
        tmp_path,
        title="Build web dashboard for task queue",
        goal="Show in progress tasks in the dashboard",
    )
    in_progress.status = "in_progress"
    save_task(tmp_path, in_progress)
    done = create_task(
        tmp_path,
        title="Build web dashboard for tasks",
        goal="Show done tasks in the dashboard",
    )
    done.status = "done"
    done.pipeline_status = "done"
    save_task(tmp_path, done)

    result = CliRunner().invoke(
        task_app,
        [
            "add",
            "Build web dashboard for task tracking",
            "--workspace",
            str(tmp_path),
            "--goal",
            "Show queued in progress and done tasks in the dashboard",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "warning: potential duplicate tasks found:" in result.output
    assert f"{queued.id} [queued] {queued.title}" in result.output
    assert f"{in_progress.id} [in_progress] {in_progress.title}" in result.output
    assert f"{done.id} [done] {done.title}" in result.output
    assert "Created task T-0004" in result.output


def test_task_add_cli_shows_done_for_legacy_archived_duplicate(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    archived = create_task(
        tmp_path,
        title="Build web dashboard for tasks",
        goal="Show done tasks in the dashboard",
    )
    archived.status = "done"
    archived.pipeline_status = "done"
    save_task(tmp_path, archived)
    archive_task(tmp_path, archived.id)

    archive_dir = tmp_path / ".litehive" / "tasks" / "archive" / f"{archived.id}-{archived.slug}"
    task_yaml = archive_dir / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    data.pop("status", None)
    data.pop("pipeline_status", None)
    task_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    result = CliRunner().invoke(
        task_app,
        [
            "add",
            "Build web dashboard for task tracking",
            "--workspace",
            str(tmp_path),
            "--goal",
            "Show queued in progress and done tasks in the dashboard",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert f"{archived.id} [archived] {archived.title}" in result.output
    assert f"{archived.id} [queued] {archived.title}" not in result.output
    assert "Created task T-0002" in result.output


def test_task_update_help_matches_trimmed_option_surface() -> None:
    result = CliRunner().invoke(task_app, ["update", "--help"], standalone_mode=False)

    assert result.exit_code == 0, result.output
    for option in [
        "--title",
        "--priority",
        "--goal",
        "--depends-on",
        "--acceptance-criteria",
        "--constraint",
        "--plan-step",
    ]:
        assert option in result.output
    for option in [
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
        assert option not in result.output


def test_task_update_cli_persists_surviving_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")
    task = create_task(tmp_path, title="Original title")

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

    persisted = get_task(tmp_path, task.id)
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
    assert follow_up.created_from.source == "follow_up"
    assert follow_up.created_from.task_id == parent.id
    assert follow_up.created_from.stage == "accepting"
    assert follow_up.created_from.blocking is True
    assert load_state(tmp_path).queue[-1] == created[0].id


def test_task_search_cli_returns_ranked_matches_from_existing_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    best = create_task(
        tmp_path,
        title="Dashboard search rollout",
        goal="Add natural language task search backed by sqlitesearch ranking",
    )
    create_task(
        tmp_path,
        title="Dashboard search copy refresh",
        goal="Refresh dashboard helper text and labels",
    )
    rebuild_duplicate_task_index(tmp_path)

    def fail_if_rebuilt(root: Path) -> list[object]:
        raise AssertionError("search unexpectedly rebuilt the duplicate index")

    monkeypatch.setattr("litehive.tasks.duplicates._iter_indexable_tasks", fail_if_rebuilt)

    result = CliRunner().invoke(
        task_app,
        [
            "search",
            "dashboard search sqlitesearch",
            "--workspace",
            str(tmp_path),
            "--limit",
            "2",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert result.output.index(best.id) < result.output.index("T-0002")
    assert f"{best.id} [queued] Dashboard search rollout" in result.output
    assert "goal: Add natural language task search backed by sqlitesearch ranking" in result.output


def test_task_search_cli_handles_empty_results(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Dashboard rollout", goal="Ship the dashboard experience")

    result = CliRunner().invoke(
        task_app,
        [
            "search",
            "totally unrelated phrase",
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "No matching tasks found for: totally unrelated phrase" in result.output


def test_search_tasks_by_text_tracks_duplicate_index_maintenance(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Seed task", goal="Bootstrap duplicate search")
    rebuild_duplicate_task_index(tmp_path)

    task = create_task(
        tmp_path,
        title="Dashboard cleanup",
        goal="Remove duplicate web dashboard cards",
    )

    created_matches = search_tasks_by_text(tmp_path, query="duplicate dashboard cards", limit=10)
    created_match = next(match for match in created_matches if match.task_id == task.id)
    assert created_match.title == "Dashboard cleanup"
    assert "duplicate web dashboard cards" in created_match.snippet
    assert created_match.status == "queued"

    update_task_metadata(
        tmp_path,
        task.id,
        title="Dashboard duplicate cleanup",
        goal="Remove duplicate web dashboard widgets",
    )
    updated_matches = search_tasks_by_text(tmp_path, query="duplicate dashboard widgets", limit=10)
    updated_match = next(match for match in updated_matches if match.task_id == task.id)
    assert updated_match.title == "Dashboard duplicate cleanup"
    assert "duplicate web dashboard widgets" in updated_match.snippet

    task = get_task(tmp_path, task.id)
    assert task is not None
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    archive_task(tmp_path, task.id)

    archived_matches = search_tasks_by_text(tmp_path, query="duplicate dashboard widgets", limit=10)
    archived_match = next(match for match in archived_matches if match.task_id == task.id)
    assert archived_match.status == "archived"
    assert archived_match.title == "Dashboard duplicate cleanup"
    assert "duplicate web dashboard widgets" in archived_match.snippet
