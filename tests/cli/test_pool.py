from litehive.cli.pool import (
    collect_task_stage_stats,
    compute_pool_flow_statistics,
    task_stage_outcomes,
)
from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import StageReport
from litehive.state.records import create_task
from litehive.tasks.reports import record_stage_report


def test_pool_reads_canonical_stage_reports(tmp_path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Pool stage metrics")
    record_stage_report(
        tmp_path,
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
    assert collect_task_stage_stats(tmp_path, task.id) == [
        {"stage": "implementing", "verdict": "pass", "duration_seconds": 12.0}
    ]


def test_pool_flow_statistics_use_canonical_stage_keys(tmp_path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Pool flow stats")
    record_stage_report(
        tmp_path,
        task,
        StageReport(
            task_id=task.id,
            pipeline_state="implementing",
            verdict="pass",
            summary="implemented change",
            duration_seconds=12,
        ),
    )

    flow_statistics = compute_pool_flow_statistics(tmp_path, [{"task_id": task.id}])

    assert flow_statistics is not None
    assert flow_statistics["stages_executed"] == 1
    assert flow_statistics["stage_metrics"]["implementing"] == {
        "avg_seconds": 12.0,
        "min_seconds": 12.0,
        "max_seconds": 12.0,
    }
    assert flow_statistics["stage_pass_counts"] == {"implementing": 1}
    assert flow_statistics["stage_fail_counts"] == {}
    assert flow_statistics["bottleneck_stage"] == "implementing"
    assert flow_statistics["bottleneck_avg_seconds"] == 12.0
