from pathlib import Path

import yaml

from litehive.config.workspace import ensure_workspace
from litehive.observability.status import render_task_summary
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

