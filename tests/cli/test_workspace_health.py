from pathlib import Path
from types import SimpleNamespace

from heru import ENGINE_CHOICES
from heru.quota import UsageStatus, UsageWindow
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.cli.workspace import (
    collect_quota_health,
    health_daemon_status,
    quota_health,
    repair_summary_lines,
    status_command,
)
from litehive.config.paths import workspace_path
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import UnmergedWorktree, WorkspaceState
from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.state.persist import load_state, save_state, save_state_without_runner_guard
from litehive.state.records import create_task, require_task, save_task

_RUNNER = CliRunner()


def test_health_daemon_status_defaults_to_stopped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("litehive.cli.workspace.daemon_metadata", lambda root: None)

    assert health_daemon_status(tmp_path) == ("stopped", "-")


def test_health_daemon_status_reports_running_pid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "litehive.cli.workspace.daemon_metadata",
        lambda root: {"status": "running", "pid": 4242},
    )

    assert health_daemon_status(tmp_path) == ("running", "4242")


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
        "stale_unmerged_worktrees_removed: 0",
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
        "stale_unmerged_worktrees_removed: 0",
        "cleared_active_task_id: -",
        "requeued_tasks: -",
        "stale_process_tasks: -",
    ]


def test_quota_health_formats_status_and_reset() -> None:
    status = UsageStatus(
        limit_reached=True,
        short_term=UsageWindow(percent_remaining=12.5, reset_at="2026-04-14T12:00:00Z"),
        long_term=UsageWindow(percent_remaining=45.0, reset_at="2026-04-15T00:00:00Z"),
    )

    health = quota_health("codex", status, reset_at="2026-04-15T00:00:00Z")

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
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Repair stale resumable task")
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "idle"
    task.runtime.current_stage.stage = "testing"
    task.runtime.current_stage.status = "idle"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = []
    save_state(tmp_path, state)

    result = _RUNNER.invoke(app, ["doctor", "--workspace", str(tmp_path)], standalone_mode=False)

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
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.current_stage.stage == "testing"
    assert refreshed.runtime.current_stage.status == "idle"
    assert load_state(tmp_path).queue == [task.id]


def test_repair_skips_legacy_disk_only_tasks_missing_runtime_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Repair stale resumable task")
    legacy = create_task(tmp_path, title="Legacy disk-only task")

    with connect_workspace_db(tmp_path) as connection:
        connection.execute("DELETE FROM task_state WHERE task_id = ?", (legacy.id,))

    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "idle"
    task.runtime.current_stage.stage = "testing"
    task.runtime.current_stage.status = "idle"
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


def test_repair_removes_stale_unmerged_worktrees_and_reports_count(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    done_task = create_task(tmp_path, title="Done rescued task")
    done_task.status = "done"
    done_task.pipeline_status = "done"
    save_task(tmp_path, done_task)

    queued_task = create_task(tmp_path, title="Queued missing worktree task")
    queued_task.status = "queued"
    queued_task.pipeline_status = "implementing"
    save_task(tmp_path, queued_task)

    existing_worktree = workspace_path(tmp_path, "worktrees") / f"{done_task.id}-{done_task.slug}"
    existing_worktree.mkdir(parents=True, exist_ok=True)
    missing_worktree = workspace_path(tmp_path, "worktrees") / f"{queued_task.id}-{queued_task.slug}"

    state = load_state(tmp_path)
    state.unmerged_worktrees = [
        UnmergedWorktree(task_id=done_task.id, worktree_path=str(existing_worktree)),
        UnmergedWorktree(task_id=queued_task.id, worktree_path=str(missing_worktree)),
    ]
    save_state_without_runner_guard(tmp_path, state)

    result = _RUNNER.invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 0
    assert "repaired: yes" in result.output
    assert "stale_unmerged_worktrees_removed: 2" in result.output

    refreshed = load_state(tmp_path)
    assert refreshed.unmerged_worktrees == []

    rerun = _RUNNER.invoke(app, ["doctor", "--workspace", str(tmp_path)], standalone_mode=False)

    assert rerun.return_value == 0
    assert "repaired: no" in rerun.output
    assert "stale_unmerged_worktrees_removed: 0" in rerun.output


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
                last_subagent=None,
            ),
        ),
    )
    status.active_task = active_task

    monkeypatch.setattr("litehive.cli.workspace.collect_task_pipeline_status", lambda root: status)
    monkeypatch.setattr("litehive.cli.workspace.list_tasks_state_first", lambda workspace, state=None: [])
    monkeypatch.setattr("litehive.cli.workspace.find_last_completed_task", lambda tasks: None)
    monkeypatch.setattr("litehive.cli.workspace.collect_recent_activity", lambda root: [])
    monkeypatch.setattr("litehive.cli.workspace.render_engine_health_section", lambda monitoring: [])
    monkeypatch.setattr("litehive.cli.workspace.render_engine_monitoring_lines", lambda monitoring: [])
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

    monkeypatch.setattr("litehive.cli.workspace.collect_task_pipeline_status", lambda root: status)
    monkeypatch.setattr(
        "litehive.cli.workspace.render_task_pipeline_status_lines",
        lambda task_status, *, workspace, mode, retry_on_label=None: ["workspace: demo"],
    )

    def fake_list_tasks(workspace, *, strict=True):
        captured["strict"] = strict
        return []

    monkeypatch.setattr("litehive.cli.workspace.list_tasks", fake_list_tasks)
    monkeypatch.setattr("litehive.cli.workspace.print_status_issues", lambda issues: 0)

    exit_code = status_command(tmp_path, full=True)

    assert exit_code == 0
    assert captured["strict"] is False
