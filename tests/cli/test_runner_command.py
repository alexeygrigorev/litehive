from pathlib import Path

from typer.testing import CliRunner

from litehive.cli.app import app as root_app
from litehive.config.workspace import ensure_workspace
from litehive.domain.task import TaskRecord
from litehive.state.records import save_task
from litehive.tasks.queue import enqueue_task


def test_run_accepts_dry_run_flag(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    result = CliRunner().invoke(
        root_app,
        ["run", "--dry-run", "--workspace", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "No queued task." in result.output


def test_run_dry_run_shows_task_info(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    # Create and enqueue a test task
    task = TaskRecord(
        id="T-TEST",
        slug="test-task",
        title="Test Task for Dry Run",
        pipeline_status="implementing",
    )
    save_task(tmp_path, task)
    enqueue_task(tmp_path, task.id)

    result = CliRunner().invoke(
        root_app,
        ["run", "--dry-run", "--workspace", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "task: T-TEST Test Task for Dry Run" in result.output
    assert "stage: backlog" in result.output
    assert "effective_engine:" in result.output
    assert "effective_model:" in result.output


def test_run_dry_run_shows_model_override(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    # Create and enqueue a test task
    task = TaskRecord(
        id="T-TEST",
        slug="test-task",
        title="Test Task for Dry Run",
        pipeline_status="implementing",
        model="test-model",
    )
    save_task(tmp_path, task)
    enqueue_task(tmp_path, task.id)

    result = CliRunner().invoke(
        root_app,
        ["run", "--dry-run", "--model", "override-model", "--workspace", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "task: T-TEST Test Task for Dry Run" in result.output
    assert "effective_model: override-model" in result.output
