from pathlib import Path

import yaml

from litehive.config.workspace import ensure_workspace
from litehive.domain.runtime import RuntimeSubagentState
from litehive.observability.status import render_active_task_detail_lines, render_task_summary
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
