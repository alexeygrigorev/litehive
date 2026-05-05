from litehive.cli.pool import _write_pool_summary_report, task_stage_outcomes
from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import StageReport
from litehive.state.records import create_task
from litehive.tasks.report_storage import record_stage_report
from litehive.workspace import Workspace


def test_pool_reads_canonical_stage_reports(tmp_path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Pool stage metrics")
    record_stage_report(Workspace.from_path(tmp_path),
        task,
        StageReport(
            task_id=task.id,
            pipeline_state="implementing",
            verdict="pass",
            summary="implemented change",
            duration_seconds=12,
        ),
    )

    assert task_stage_outcomes(tmp_path, task.id, task.slug) == ["implementing=pass"]
    assert list((tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports").glob("*.yaml")) == []


def test_pool_summary_writes_operator_text_without_structured_yaml(tmp_path) -> None:
    ensure_workspace(tmp_path)
    report = {
        "created_at": "2026-04-25T00:00:00Z",
        "completed_count": 0,
        "completed": [],
        "flagged_count": 0,
        "flagged": [],
        "resumable_count": 0,
        "resumable": [],
        "closed_count": 0,
        "closed": [],
        "skipped_count": 0,
        "skipped": [],
        "remaining_count": 0,
        "remaining": [],
        "tasks_run": 0,
        "stop_condition": "queue exhausted",
        "stop_reason": "queue_exhausted",
    }

    _write_pool_summary_report(root=tmp_path, report=report)

    summary_path = tmp_path / ".litehive" / "pool-summary.txt"
    summary_text = summary_path.read_text(encoding="utf-8")
    assert "completed_tasks: 0" in summary_text
    assert "flow_statistics:" not in summary_text
    assert list((tmp_path / ".litehive" / "logs" / "pool-runs").glob("*.yaml")) == []
