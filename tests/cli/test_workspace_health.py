from pathlib import Path

from litehive.cli.workspace import _health_daemon_status, _repair_summary_lines
from litehive.domain.task_ops import WorkspaceRepairSummary


def test_health_daemon_status_defaults_to_stopped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("litehive.cli.workspace.daemon_metadata", lambda root: None)

    assert _health_daemon_status(tmp_path) == ("stopped", "-")


def test_health_daemon_status_reports_running_pid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "litehive.cli.workspace.daemon_metadata",
        lambda root: {"status": "running", "pid": 4242},
    )

    assert _health_daemon_status(tmp_path) == ("running", "4242")


def test_repair_summary_lines_omit_empty_fields_for_doctor_mode() -> None:
    summary = WorkspaceRepairSummary(
        mutated=True,
        stale_runner_recovered=True,
        requeued_task_ids=["T-0002"],
    )

    lines = _repair_summary_lines(
        summary,
        result_label="doctor_repaired",
        include_empty=False,
        include_extended_fields=False,
    )

    assert lines == [
        "doctor_repaired: yes",
        "stale_runner_recovered: yes",
        "requeued_tasks: T-0002",
    ]


def test_repair_summary_lines_include_empty_fields_for_repair_mode() -> None:
    summary = WorkspaceRepairSummary()

    lines = _repair_summary_lines(
        summary,
        result_label="repaired",
        include_empty=True,
        include_extended_fields=True,
    )

    assert lines == [
        "repaired: no",
        "stale_runner_recovered: no",
        "cleared_active_task_id: -",
        "requeued_tasks: -",
        "removed_queue_entries: -",
        "deduped_queue_entries: -",
        "restored_queue_entries: -",
        "finalized_commit_tasks: -",
        "stale_process_tasks: -",
        "reassigned_duplicate_ids: -",
    ]
