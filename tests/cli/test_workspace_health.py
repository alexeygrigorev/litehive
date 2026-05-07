from pathlib import Path
from types import SimpleNamespace

from heru import ENGINE_CHOICES
from heru.quota import UsageStatus, UsageWindow
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.cli.workspace import (
    collect_quota_health,
    health_daemon_status_for_workspace,
    quota_health,
    repair_summary_lines,
    status_command,
)
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import create_workspace
from litehive.daemon.registry import DaemonRegistryEntry
from litehive.db.schema import connect_workspace_db
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.reports import StageReport
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import WorkspaceState
from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, require_task, save_task
from litehive.tasks.report_storage import record_stage_report
from litehive.workspace import Workspace
from litehive.domain.common import PipelineStatus, TaskStatus

_RUNNER = CliRunner()


def test_health_daemon_status_defaults_to_stopped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("litehive.cli.workspace.daemon_metadata_for_workspace", lambda workspace: None)

    assert health_daemon_status_for_workspace(Workspace.from_path(tmp_path)) == ("stopped", "-")


def test_health_daemon_status_reports_running_pid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "litehive.cli.workspace.daemon_metadata_for_workspace",
        lambda workspace: DaemonRegistryEntry(
            status="running",
            pid=4242,
            workspace=str(workspace.root),
            started_at=None,
            heartbeat_at=None,
            log_dir=None,
        ),
    )

    assert health_daemon_status_for_workspace(Workspace.from_path(tmp_path)) == ("running", "4242")


def test_repair_summary_lines_omit_empty_fields() -> None:
    summary = WorkspaceRepairSummary(
        mutated=True,
        stale_runner_recovered=True,
        requeued_task_ids=["T-0002"],
    )

    lines = repair_summary_lines(
        summary,
        result_label="repaired",
        include_empty=False,
        include_extended_fields=False,
    )

    assert lines == [
        "repaired: yes",
        "stale_runner_recovered: yes",
        "requeued_tasks: T-0002",
    ]


def test_repair_summary_lines_include_empty_fields_for_repair_mode() -> None:
    summary = WorkspaceRepairSummary()

    lines = repair_summary_lines(
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
        "stale_process_tasks: -",
        "normalized_terminal_tasks: -",
    ]


def test_doctor_command_is_not_registered(tmp_path: Path) -> None:
    create_workspace(tmp_path)

    result = _RUNNER.invoke(app, ["doctor", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.exit_code != 0
    assert result.return_value is None
    assert result.exception is not None
    assert "No such command 'doctor'." in str(result.exception)
    assert "repaired:" not in result.output


def test_quota_health_formats_status_and_reset() -> None:
    status = UsageStatus(
        limit_reached=True,
        short_term=UsageWindow(percent_remaining=12.5, reset_at="2026-04-14T12:00:00Z"),
        long_term=UsageWindow(percent_remaining=45.0, reset_at="2026-04-15T00:00:00Z"),
    )

    health = quota_health("codex", status)

    assert health.engine == "codex"
    assert health.status == "warning"
    assert health.problem is True
    assert health.summary == "hours remaining=12.5% weeks remaining=45.0% reset=2026-04-15T00:00:00Z"


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

    items = collect_quota_health()
    by_engine = {item.engine: item for item in items}

    assert [item.engine for item in items] == list(ENGINE_CHOICES)
    assert by_engine["claude"].summary.endswith("reset=2026-04-14T11:00:00Z")
    assert by_engine["codex"].summary.endswith("reset=2026-04-15T00:00:00Z")
    assert by_engine["copilot"].summary.endswith("reset=2026-04-16T00:00:00Z")
    assert by_engine["gemini"].status == "unsupported"
    assert by_engine["goz"].problem is True
    assert by_engine["opencode"].summary == "hours remaining=10.0% weeks remaining=5.0%"


def test_repair_requeues_idle_in_progress_task_into_canonical_resumable_state(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Repair stale resumable task")
    task.status = TaskStatus.IN_PROGRESS
    task.pipeline_status = PipelineStatus.TESTING
    task.runtime.pipeline.execution_status = "idle"
    task.runtime.pipeline.current_stage.stage = "testing"
    task.runtime.pipeline.current_stage.status = "idle"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = []
    save_state(tmp_path, state)

    result = _RUNNER.invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 0
    assert "repaired: yes" in result.output
    assert "stale_runner_recovered: yes" in result.output
    assert f"cleared_active_task_id: {task.id}" in result.output
    assert f"requeued_tasks: {task.id}" in result.output
    assert "active_task_id: None" in result.output
    assert "queue_length: 1" in result.output

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.pipeline.execution_status == "idle"
    assert refreshed.runtime.pipeline.current_stage.stage == "testing"
    assert refreshed.runtime.pipeline.current_stage.status == "idle"
    assert load_state(tmp_path).queue == [task.id]


def test_repair_skips_legacy_disk_only_tasks_missing_runtime_state(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Repair stale resumable task")
    legacy = create_task(tmp_path, title="Legacy disk-only task")

    with connect_workspace_db(tmp_path) as connection:
        connection.execute("DELETE FROM task_state WHERE task_id = ?", (legacy.id,))

    task.status = TaskStatus.IN_PROGRESS
    task.pipeline_status = PipelineStatus.TESTING
    task.runtime.pipeline.execution_status = "idle"
    task.runtime.pipeline.current_stage.stage = "testing"
    task.runtime.pipeline.current_stage.status = "idle"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = []
    save_state(tmp_path, state)

    result = _RUNNER.invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 0
    assert "repaired: yes" in result.output
    assert f"requeued_tasks: {task.id}" in result.output
    assert load_state(tmp_path).queue == [task.id]
    refreshed = require_task(tmp_path, task.id)
    assert refreshed.status == "queued"


def test_repair_normalizes_stale_queued_terminal_task(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Already accepted task")
    task.status = TaskStatus.QUEUED
    task.pipeline_status = PipelineStatus.BACKLOG
    task.runtime.pipeline.execution_status = "interrupted"
    task.runtime.pipeline.current_stage.stage = "backlog"
    task.runtime.pipeline.current_stage.status = "idle"
    save_task(tmp_path, task)
    record_stage_report(Workspace.from_path(tmp_path),
        task,
        StageReport(
            task_id=task.id,
            pipeline_state="accepting",
            verdict="pass",
            summary="Acceptance passed before stale state recovery.",
        ),
    )

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = []
    save_state(tmp_path, state)

    result = _RUNNER.invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 0
    assert "repaired: yes" in result.output
    assert f"normalized_terminal_tasks: {task.id}" in result.output
    assert "queue_length: 0" in result.output

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.close_reason == "done"
    assert refreshed.runtime.pipeline.execution_status == "done"
    assert refreshed.runtime.pipeline.current_stage.stage == "done"


def test_status_command_prefers_runner_active_task_id(tmp_path: Path, monkeypatch, capsys) -> None:
    status = SimpleNamespace(
        config=LitehiveConfig(default_engine="codex"),
        state=WorkspaceState(active_task_id=None, queue=["T-0382"]),
        monitoring=WorkspaceEngineMonitoring(),
        active_task_id="T-0381",
        waiting_lines=[],
        issues=[],
        runner=RunnerStatusState(
            status="running",
            pid=123,
            started_at="2026-04-16T03:15:43Z",
            heartbeat_at="2026-04-16T03:21:53Z",
            active_task_id="T-0381",
        ),
    )
    active_task = SimpleNamespace(
        id="T-0381",
        title="Move stage and recovery reports off YAML storage",
        pipeline_status="implementing",
        current_pipeline_stage="implementing",
        subagents=[],
        runtime=SimpleNamespace(
            pipeline=SimpleNamespace(
                run_started_at="2026-04-16T03:15:43Z",
                current_stage=SimpleNamespace(
                    stage="implementing",
                    started_at="2026-04-16T03:20:00Z",
                    duration_seconds=0,
                ),
            ),
            execution=SimpleNamespace(
                active_subagent=None,
            ),
        ),
    )
    status.active_task = active_task

    monkeypatch.setattr("litehive.cli.workspace.collect_task_pipeline_status_for_workspace", lambda workspace, **kwargs: status)
    monkeypatch.setattr("litehive.cli.workspace.list_tasks_state_first", lambda workspace, state=None: [])
    monkeypatch.setattr("litehive.cli.workspace.find_last_completed_task", lambda tasks: None)
    monkeypatch.setattr("litehive.cli.workspace.collect_recent_activity", lambda root: [])
    monkeypatch.setattr("litehive.cli.workspace.render_recent_activity_section", lambda events: [])
    monkeypatch.setattr("litehive.cli.workspace.print_status_issues", lambda issues: 0)

    exit_code = status_command(tmp_path)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "T-0381 implementing with codex" in output


def test_full_status_command_lists_tasks_with_strict_false(tmp_path: Path, monkeypatch) -> None:
    status = SimpleNamespace(
        config=LitehiveConfig(default_engine="codex"),
        active_task_id="T-0381",
        issues=[],
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr("litehive.cli.workspace.collect_task_pipeline_status_for_workspace", lambda workspace, **kwargs: status)
    monkeypatch.setattr(
        "litehive.cli.workspace.render_task_pipeline_status_lines",
        lambda task_status, *, workspace, mode, retry_on_label=None: ["workspace: demo"],
    )

    def fake_list_tasks(self, *, strict=True):
        captured["strict"] = strict
        return []

    monkeypatch.setattr("litehive.cli.workspace.Workspace.list_tasks", fake_list_tasks)
    monkeypatch.setattr("litehive.cli.workspace.print_status_issues", lambda issues: 0)

    exit_code = status_command(tmp_path, full=True)

    assert exit_code == 0
    assert captured["strict"] is False
