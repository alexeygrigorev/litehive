from pathlib import Path

import yaml

from litehive.config.workspace import ensure_workspace
from litehive.domain.runtime import RuntimeSubagentState
from litehive.recovery.workspace_repair import (
    prepare_interrupted_task,
    recover_stale_runner_state,
)
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, get_task, save_task


def _load_subagent_yaml(base: Path, name: str) -> dict[str, object]:
    return yaml.safe_load((base / name).read_text(encoding="utf-8")) or {}


def test_recover_stale_runner_state_requeues_running_stage(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Crash recovery")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.step = "implementing"
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.stage == "implementing"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task.id


def test_recover_stale_runner_state_requeues_commit_stage_as_queued(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Commit recovery")
    task.status = "in_progress"
    task.pipeline_status = "commit_to_git"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.step = "commit_to_git"
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.resume_stage == "commit_to_git"


def test_recover_stale_runner_state_requeues_running_task_without_active_task_id(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Running without active task id")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.step = "implementing"
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = None
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.runtime.execution_status == "interrupted"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.queue[0] == task.id


def test_prepare_interrupted_task_writes_resume_bookkeeping(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Interrupted run")
    subagent_base = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "subagents" / "SA-1234-swe"
    subagent_base.mkdir(parents=True)
    (subagent_base / "report.yaml").write_text("summary: finished half the change\n", encoding="utf-8")
    (subagent_base / "session.yaml").write_text("status: running\n", encoding="utf-8")

    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.step = "implementing"
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-1234",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-1234-swe",
        sandboxed=False,
        sandbox_summary="host",
        started_at="2026-04-12T10:00:00Z",
        updated_at="2026-04-12T10:05:00Z",
    )

    prepare_interrupted_task(
        tmp_path,
        task,
        stage="implementing",
        summary="Interrupted run recovered. Resume from `implementing`.",
        reason="received ctrl-c",
    )

    assert task.runtime.interruption is not None
    assert task.runtime.interruption.reason == "received ctrl-c"
    assert task.runtime.interruption.resume_stage == "implementing"
    assert task.runtime.active_subagent is None
    assert task.runtime.last_subagent is not None
    assert task.runtime.last_subagent.status == "interrupted"

    session = _load_subagent_yaml(subagent_base, "session.yaml")
    report = _load_subagent_yaml(subagent_base, "report.yaml")
    assert session["status"] == "interrupted"
    assert session["resume_stage"] == "implementing"
    assert session["interruption_reason"] == "received ctrl-c"
    assert report["status"] == "interrupted"
    assert report["resume_stage"] == "implementing"
    assert report["interruption_reason"] == "received ctrl-c"
