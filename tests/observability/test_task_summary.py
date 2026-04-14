from pathlib import Path

import yaml

from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.domain.runtime import RunnerStatusState, RuntimeSubagentState
from litehive.domain.task import WorkspaceState
from litehive.observability.status import (
    render_active_task_detail_lines,
    render_full_status_header_lines,
    render_runner_status_line,
    render_runtime_policy_lines,
    render_task_summary,
)
from litehive.state.records import create_task


def test_render_task_summary_includes_estimate_velocity_and_eta(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Estimate demo task")

    reports_dir = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "grooming-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "grooming",
                "verdict": "pass",
                "summary": "ok",
                "duration_seconds": 120,
            }
        ),
        encoding="utf-8",
    )

    task.pipeline_status = "implementing"
    lines = render_task_summary(task, active=True, root=tmp_path)
    combined = "\n".join(lines)
    assert "stage_estimate=" in combined
    assert "velocity=" in combined
    assert "eta=" in combined


def test_render_active_task_detail_lines_prefers_active_subagent_engine(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Active detail task")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.runtime.current_stage.step = "testing"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="sa-1",
        role="implementer",
        engine="codex",
        status="running",
        path="/tmp/sa-1",
        started_at="2026-04-14T10:00:00Z",
        updated_at="2026-04-14T10:00:00Z",
    )

    lines = render_active_task_detail_lines(task, "claude")

    assert lines == [
        "active_task_title: Active detail task",
        "active_task_status: in_progress/implementing",
        "active_stage: testing",
        "active_engine: codex",
    ]


def test_render_runner_status_and_full_header_lines(tmp_path: Path) -> None:
    config = LitehiveConfig(
        default_engine="codex",
        litehive_source_path="/src/litehive",
        engine_freeze={"gemini": "2099-06-15T00:00:00Z"},
    )
    state = WorkspaceState(active_task_id="T-0001", queue=["T-0002", "T-0003"])
    runner = RunnerStatusState(
        status="running",
        pid=123,
        started_at="2026-04-14T10:00:00Z",
        heartbeat_at="2026-04-14T10:01:00Z",
        active_task_id="T-0001",
    )

    runner_line = render_runner_status_line(runner)
    lines = render_full_status_header_lines(tmp_path, config, state, runner)

    assert runner_line == (
        "runner_status: running pid=123 "
        "started_at=2026-04-14T10:00:00Z "
        "heartbeat_at=2026-04-14T10:01:00Z "
        "active_task_id=T-0001"
    )
    assert lines[0] == f"workspace: {tmp_path}"
    assert "status_read_mode: full" in lines
    assert "default_engine: codex" in lines
    assert "litehive_source_path: /src/litehive" in lines
    assert "active_task_id: T-0001" in lines
    assert runner_line in lines
    assert "queued_tasks: 2" in lines


def test_render_runtime_policy_lines_uses_preformatted_retry_label() -> None:
    config = LitehiveConfig(
        default_retry_limit=5,
        pool_stop_on_failure=True,
        pool_max_tasks=7,
        pool_stop_on_dirty_git=True,
        pool_stop_on_attention=True,
        pool_selection_policy="fifo",
        process_profile="python",
    )

    lines = render_runtime_policy_lines(config, "timeout, network")

    assert lines == [
        "default_retry_limit: 5",
        "retry_on: timeout, network",
        "pool_stop_on_failure: True",
        "pool_max_tasks: 7",
        "pool_stop_on_dirty_git: True",
        "pool_stop_on_attention: True",
        "pool_selection_policy: fifo",
        "process_profile: python",
    ]
