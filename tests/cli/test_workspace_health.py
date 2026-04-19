import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from heru import ENGINE_CHOICES
from heru.quota import UsageStatus, UsageWindow
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.cli.workspace import (
    _collect_quota_health,
    _print_doctor_snapshot,
    _health_daemon_status,
    _quota_health,
    _repair_summary_lines,
    status_command,
)
from litehive.config.model import LitehiveConfig
from litehive.config.paths import worktree_root
from litehive.config.workspace import ensure_workspace
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import UnmergedWorktree, WorkspaceState
from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, save_task

_RUNNER = CliRunner()


def _write_cache_tool(cache_target: Path) -> None:
    cache_target.parent.mkdir(parents=True, exist_ok=True)
    cache_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cache_target.chmod(0o755)


def _create_broken_venv_binary(checkout_root: Path, binary_name: str, cache_root: Path) -> Path:
    bin_dir = checkout_root / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cache_target = cache_root / f"{binary_name}-tool"
    _write_cache_tool(cache_target)
    binary_path = bin_dir / binary_name
    binary_path.symlink_to(cache_target)
    cache_target.unlink()
    return binary_path


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
        "stale_unmerged_worktrees_removed: 0",
        "cleared_active_task_id: -",
        "requeued_tasks: -",
        "removed_queue_entries: -",
        "deduped_queue_entries: -",
        "broken_venv_binaries: -",
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


def test_doctor_reports_broken_workspace_and_worktree_venvs_without_claiming_fix(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    cache_root = tmp_path / "fake-home" / ".cache" / "uv"
    _create_broken_venv_binary(tmp_path, "ruff", cache_root)
    worktree_path = worktree_root(tmp_path) / "T-0001-demo"
    _create_broken_venv_binary(worktree_path, "pytest", cache_root)

    result = _RUNNER.invoke(app, ["doctor", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 1
    assert "stale_unmerged_worktrees_removed: 0" in result.output
    assert f"venv_health: BROKEN binary=ruff venv={tmp_path / '.venv'}" in result.output
    assert f"venv={worktree_path / '.venv'} checkout={worktree_path}" in result.output
    assert "uv venv --clear .venv && uv sync --extra dev" in result.output

    fix_result = _RUNNER.invoke(app, ["doctor", "--fix", "--workspace", str(tmp_path)], standalone_mode=False)

    assert fix_result.return_value == 1
    assert "stale_unmerged_worktrees_removed: 0" in fix_result.output
    assert "doctor_repaired: no" in fix_result.output
    assert f"broken_venv_binaries: {tmp_path / '.venv'}:ruff {worktree_path / '.venv'}:pytest" in fix_result.output
    assert "uv venv --clear .venv && uv sync --extra dev" in fix_result.output


def test_doctor_removes_stale_unmerged_worktree_entries(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    done_task = create_task(tmp_path, title="Done worktree cleanup")
    done_task.status = "done"
    done_task.pipeline_status = "done"
    save_task(tmp_path, done_task)

    queued_task = create_task(tmp_path, title="Missing worktree cleanup")

    existing_worktree = worktree_root(tmp_path) / f"{done_task.id}-{done_task.slug}"
    existing_worktree.mkdir(parents=True)
    missing_worktree = worktree_root(tmp_path) / f"{queued_task.id}-{queued_task.slug}"

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = []
    state.unmerged_worktrees = [
        UnmergedWorktree(task_id=done_task.id, worktree_path=str(existing_worktree.resolve())),
        UnmergedWorktree(task_id=queued_task.id, worktree_path=str(missing_worktree.resolve())),
    ]
    save_state(tmp_path, state)

    result = _RUNNER.invoke(app, ["doctor", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 0
    assert "stale_unmerged_worktrees_removed: 2" in result.output
    assert f"doctor: clean workspace={tmp_path}" in result.output
    assert load_state(tmp_path).unmerged_worktrees == []

    clean_result = _RUNNER.invoke(app, ["doctor", "--workspace", str(tmp_path)], standalone_mode=False)

    assert clean_result.return_value == 0
    assert "stale_unmerged_worktrees_removed: 0" in clean_result.output
    assert f"doctor: clean workspace={tmp_path}" in clean_result.output


def test_documented_clear_and_sync_fix_restores_broken_symlink_after_uv_cache_clean(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "workspace"
    cache_dir = tmp_path / "uv-cache"
    workspace.mkdir()
    shutil.copy(repo_root / "pyproject.toml", workspace / "pyproject.toml")
    shutil.copy(repo_root / "uv.lock", workspace / "uv.lock")
    shutil.copy(repo_root / "README.md", workspace / "README.md")
    shutil.copytree(repo_root / "litehive", workspace / "litehive")
    shutil.copytree(repo_root / "heru", workspace / "heru")
    ensure_workspace(workspace)

    env = {
        **os.environ,
        "UV_CACHE_DIR": str(cache_dir),
        "UV_LINK_MODE": "symlink",
    }

    subprocess.run(["uv", "sync", "--extra", "dev"], cwd=workspace, env=env, check=True, capture_output=True, text=True)
    ruff_path = workspace / ".venv" / "bin" / "ruff"
    assert ruff_path.is_symlink()

    subprocess.run(["uv", "cache", "clean"], cwd=workspace, env=env, check=True, capture_output=True, text=True)
    try:
        broken = subprocess.run([str(ruff_path), "--version"], cwd=workspace, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        broken = None
    assert broken is None or broken.returncode != 0

    doctor = _RUNNER.invoke(app, ["doctor", "--workspace", str(workspace)], standalone_mode=False)
    assert doctor.return_value == 1
    assert "uv venv --clear .venv && uv sync --extra dev" in doctor.output

    subprocess.run(["uv", "venv", "--clear", ".venv"], cwd=workspace, env=env, check=True, capture_output=True, text=True)
    subprocess.run(["uv", "sync", "--extra", "dev"], cwd=workspace, env=env, check=True, capture_output=True, text=True)
    repaired = subprocess.run([str(ruff_path), "--version"], cwd=workspace, capture_output=True, text=True, check=False)

    assert repaired.returncode == 0
    assert repaired.stdout.startswith("ruff ")


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
    monkeypatch.setattr(
        "litehive.cli.workspace._safe_active_task",
        lambda workspace, task_id: active_task if task_id == "T-0381" else None,
    )
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
