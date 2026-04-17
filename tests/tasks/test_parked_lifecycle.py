from pathlib import Path

import yaml
from typer.testing import CliRunner

from litehive.cli.app import app as cli_app
from litehive.config.workspace import ensure_workspace
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, get_task, list_tasks, save_task
from litehive.tasks.queue import restore_missing_queued_tasks
from litehive.tasks.status import stop_current_task
from litehive.tasks.worktrees import inspect_dirty_worktree_gate


def _set_running_task(task, *, stage: str = "implementing") -> None:
    task.status = "in_progress"
    task.pipeline_status = stage
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.stage = stage
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"


def test_stop_current_task_marks_active_work_as_parked(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Stop me later",
        acceptance_criteria=["resume from current stage"],
    )
    _set_running_task(task)
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = []
    save_state(tmp_path, state)

    summary = stop_current_task(tmp_path)

    assert summary.task.status == "parked"
    assert summary.task.pipeline_status == "implementing"
    assert summary.task.runtime.interruption is not None
    assert summary.task.runtime.interruption.reason == "Task stopped via CLI"

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "parked"
    assert load_state(tmp_path).active_task_id is None
    assert task.id not in load_state(tmp_path).queue


def test_restore_missing_queued_tasks_skips_parked_and_restores_interrupted(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    interrupted = create_task(
        tmp_path,
        title="Interrupted work",
        acceptance_criteria=["resume interrupted work"],
    )
    interrupted.status = "interrupted"
    interrupted.pipeline_status = "implementing"
    save_task(tmp_path, interrupted)

    parked = create_task(
        tmp_path,
        title="Parked work",
        acceptance_criteria=["resume parked work explicitly"],
    )
    parked.status = "parked"
    parked.pipeline_status = "implementing"
    save_task(tmp_path, parked)

    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    tasks_by_id = {task.id: task for task in list_tasks(tmp_path)}
    restored = restore_missing_queued_tasks(state, tasks_by_id)

    assert restored == [interrupted.id]
    assert state.queue == [interrupted.id]
    assert parked.id not in state.queue


def test_dirty_worktree_gate_only_auto_attributes_interrupted_tasks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Dirty ownership",
        acceptance_criteria=["allow resume with owned dirty paths"],
    )
    reports_dir = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    (reports_dir / "implementing-001.yaml").write_text(
        yaml.safe_dump({"files_changed": ["src/app.py"]}, sort_keys=False),
        encoding="utf-8",
    )

    monkeypatch.setattr("litehive.tasks.worktrees.is_git_repo", lambda root: True)

    def _status_porcelain(path: Path) -> list[str]:
        if Path(path).resolve() == tmp_path.resolve():
            return [" M src/app.py"]
        return []

    monkeypatch.setattr("litehive.tasks.worktrees.status_porcelain", _status_porcelain)

    task.status = "interrupted"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)
    interrupted_report = inspect_dirty_worktree_gate(tmp_path)

    assert interrupted_report.blocks_pool is False
    assert interrupted_report.findings[0].ownership == "task-owned"
    assert interrupted_report.findings[0].task_id == task.id

    task.status = "parked"
    save_task(tmp_path, task)
    parked_report = inspect_dirty_worktree_gate(tmp_path)

    assert parked_report.blocks_pool is True
    assert parked_report.findings[0].ownership == "main-checkout"
    assert parked_report.findings[0].task_id is None


def test_queue_resume_and_requeue_keep_parked_semantics_explicit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    runner = CliRunner()

    task = create_task(
        tmp_path,
        title="Queued later",
        acceptance_criteria=["resume from testing", "requeue from implementing"],
    )
    task.status = "parked"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    queue_result = runner.invoke(
        cli_app,
        ["queue", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )
    assert queue_result.exit_code == 0, queue_result.output
    assert f"resume 1. {task.id} [parked/testing]" in queue_result.output

    resume_result = runner.invoke(
        cli_app,
        ["queue", "resume", task.id, "--workspace", str(tmp_path)],
        standalone_mode=False,
    )
    assert resume_result.exit_code == 0, resume_result.output
    assert "status: queued" in resume_result.output
    assert "pipeline_stage: testing" in resume_result.output

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"

    refreshed.status = "parked"
    refreshed.pipeline_status = "testing"
    save_task(tmp_path, refreshed)
    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    requeue_result = runner.invoke(
        cli_app,
        ["queue", "requeue", task.id, "--workspace", str(tmp_path)],
        standalone_mode=False,
    )
    assert requeue_result.exit_code == 0, requeue_result.output
    assert "status: queued" in requeue_result.output
    assert "pipeline_stage: implementing" in requeue_result.output

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
