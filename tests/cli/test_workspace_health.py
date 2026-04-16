from pathlib import Path
from types import SimpleNamespace

from heru import ENGINE_CHOICES
from heru.quota import UsageStatus, UsageWindow

from litehive.cli.workspace import (
    _collect_quota_health,
    _print_doctor_snapshot,
    _health_daemon_status,
    _quota_health,
    _repair_summary_lines,
    status_command,
)
from litehive.config.model import LitehiveConfig
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import WorkspaceState
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


def test_quota_health_formats_status_and_reset() -> None:
    status = UsageStatus(
        limit_reached=True,
        short_term=UsageWindow(percent_remaining=12.5, reset_at="2026-04-14T12:00:00Z"),
        long_term=UsageWindow(percent_remaining=45.0, reset_at="2026-04-15T00:00:00Z"),
    )

    health = _quota_health("codex", status, reset_at="2026-04-15T00:00:00Z")

    assert health.engine == "codex"
    assert health.status == "warning"
    assert health.problem is True
    assert health.summary == "short=12.5% remaining long=45.0% remaining reset=2026-04-15T00:00:00Z"


def test_collect_quota_health_reuses_shared_statuses(monkeypatch) -> None:
    claude_status = UsageStatus(
        short_term=UsageWindow(percent_remaining=80.0, reset_at="2026-04-14T11:00:00Z"),
        long_term=UsageWindow(percent_remaining=60.0),
    )
    codex_status = UsageStatus(
        short_term=UsageWindow(percent_remaining=70.0),
        long_term=UsageWindow(percent_remaining=50.0, reset_at="2026-04-15T00:00:00Z"),
    )
    copilot_status = UsageStatus(
        short_term=UsageWindow(percent_remaining=65.0),
        long_term=UsageWindow(percent_remaining=40.0, reset_at="2026-04-16T00:00:00Z"),
    )
    zai_status = UsageStatus(
        limit_reached=True,
        short_term=UsageWindow(percent_remaining=10.0),
        long_term=UsageWindow(percent_remaining=5.0),
    )

    monkeypatch.setattr("litehive.cli.workspace.check_claude_quota", lambda: claude_status)
    monkeypatch.setattr("litehive.cli.workspace.check_codex_quota", lambda: codex_status)
    monkeypatch.setattr("litehive.cli.workspace.check_copilot_quota", lambda: copilot_status)
    monkeypatch.setattr("litehive.cli.workspace.check_zai_quota", lambda: zai_status)

    items = _collect_quota_health()
    by_engine = {item.engine: item for item in items}

    assert [item.engine for item in items] == list(ENGINE_CHOICES)
    assert by_engine["claude"].summary.endswith("reset=2026-04-14T11:00:00Z")
    assert by_engine["codex"].summary.endswith("reset=2026-04-15T00:00:00Z")
    assert by_engine["copilot"].summary.endswith("reset=2026-04-16T00:00:00Z")
    assert by_engine["gemini"].status == "unsupported"
    assert by_engine["goz"].problem is True
    assert by_engine["opencode"].summary == "short=10.0% remaining long=5.0% remaining"


def test_print_doctor_snapshot_reports_clean_workspace(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "litehive.cli.workspace.collect_status_snapshot",
        lambda root: type("Snapshot", (), {"issues": []})(),
    )

    exit_code = _print_doctor_snapshot(tmp_path)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"doctor: clean workspace={tmp_path}" in output


def test_status_command_prefers_runner_active_task_id(tmp_path: Path, monkeypatch, capsys) -> None:
    snapshot = SimpleNamespace(
        config=LitehiveConfig(default_engine="codex"),
        state=WorkspaceState(active_task_id=None, queue=["T-0382"]),
        runner=RunnerStatusState(
            status="running",
            pid=123,
            started_at="2026-04-16T03:15:43Z",
            heartbeat_at="2026-04-16T03:21:53Z",
            active_task_id="T-0381",
        ),
        monitoring=WorkspaceEngineMonitoring(),
        issues=[],
    )
    active_task = SimpleNamespace(
        id="T-0381",
        title="Move stage and recovery reports off YAML storage",
        pipeline_status="implementing",
        runtime=SimpleNamespace(
            active_subagent=None,
            last_subagent=None,
            run_started_at="2026-04-16T03:15:43Z",
            current_stage=SimpleNamespace(
                stage="implementing",
                started_at="2026-04-16T03:20:00Z",
                duration_seconds=0,
            ),
        ),
    )

    monkeypatch.setattr("litehive.cli.workspace.collect_status_snapshot", lambda root: snapshot)
    monkeypatch.setattr("litehive.cli.workspace._safe_active_task", lambda workspace, task_id: active_task if task_id == "T-0381" else None)
    monkeypatch.setattr("litehive.cli.workspace.list_tasks_state_first", lambda workspace, state=None: [])
    monkeypatch.setattr("litehive.cli.workspace.find_last_completed_task", lambda tasks: None)
    monkeypatch.setattr("litehive.cli.workspace.waiting_for_you_lines", lambda root: [])
    monkeypatch.setattr("litehive.cli.workspace.collect_recent_activity", lambda root: [])
    monkeypatch.setattr("litehive.cli.workspace.render_engine_health_section", lambda monitoring: [])
    monkeypatch.setattr("litehive.cli.workspace.render_engine_monitoring_lines", lambda monitoring: [])
    monkeypatch.setattr("litehive.cli.workspace.render_recent_activity_section", lambda events: [])
    monkeypatch.setattr("litehive.cli.workspace._print_status_issues", lambda issues: 0)

    exit_code = status_command(tmp_path)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "T-0381 implementing with codex" in output
