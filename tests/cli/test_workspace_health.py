import os
from pathlib import Path
import shutil
import subprocess
import tomllib
from types import SimpleNamespace

from heru import ENGINE_CHOICES
from heru.quota import UsageStatus, UsageWindow
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.cli.workspace import (
    _collect_quota_health,
    _health_daemon_status,
    _quota_health,
    _repair_summary_lines,
    status_command,
)
from litehive.config.model import LitehiveConfig
from litehive.config.paths import workspace_path
from litehive.config.workspace import ensure_workspace
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import WorkspaceState
from litehive.domain.task_ops import WorkspaceRepairSummary

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


def _copy_heru_dependency(repo_root: Path, workspace: Path) -> None:
    pyproject_data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    heru_source = (((pyproject_data.get("tool") or {}).get("uv") or {}).get("sources") or {}).get("heru")
    assert isinstance(heru_source, dict)
    configured_path = heru_source.get("path")
    assert isinstance(configured_path, str) and configured_path
    source_root = (repo_root / configured_path).resolve()
    destination = (workspace / configured_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, destination)


def test_health_daemon_status_defaults_to_stopped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("litehive.cli.workspace.daemon_metadata", lambda root: None)

    assert _health_daemon_status(tmp_path) == ("stopped", "-")


def test_health_daemon_status_reports_running_pid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "litehive.cli.workspace.daemon_metadata",
        lambda root: {"status": "running", "pid": 4242},
    )

    assert _health_daemon_status(tmp_path) == ("running", "4242")


def test_repair_summary_lines_omit_empty_fields() -> None:
    summary = WorkspaceRepairSummary(
        mutated=True,
        stale_runner_recovered=True,
        requeued_task_ids=["T-0002"],
    )

    lines = _repair_summary_lines(
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
        "broken_venv_binaries: -",
        "stale_process_tasks: -",
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

    items = _collect_quota_health()
    by_engine = {item.engine: item for item in items}

    assert [item.engine for item in items] == list(ENGINE_CHOICES)
    assert by_engine["claude"].summary.endswith("reset=2026-04-14T11:00:00Z")
    assert by_engine["codex"].summary.endswith("reset=2026-04-15T00:00:00Z")
    assert by_engine["copilot"].summary.endswith("reset=2026-04-16T00:00:00Z")
    assert by_engine["gemini"].status == "unsupported"
    assert by_engine["goz"].problem is True
    assert by_engine["opencode"].summary == "hours remaining=10.0% weeks remaining=5.0%"


def test_repair_reports_broken_workspace_and_worktree_venvs(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    cache_root = tmp_path / "fake-home" / ".cache" / "uv"
    _create_broken_venv_binary(tmp_path, "ruff", cache_root)
    worktree_path = workspace_path(tmp_path, "worktrees") / "T-0001-demo"
    _create_broken_venv_binary(worktree_path, "pytest", cache_root)

    result = _RUNNER.invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 1
    assert "repaired: no" in result.output
    assert "stale_runner_recovered: no" in result.output
    assert f"venv_health: BROKEN binary=ruff venv={tmp_path / '.venv'}" in result.output
    assert f"venv={worktree_path / '.venv'} checkout={worktree_path}" in result.output
    assert f"broken_venv_binaries: {tmp_path / '.venv'}:ruff {worktree_path / '.venv'}:pytest" in result.output
    assert "uv venv --clear .venv && uv sync --extra dev" in result.output


def test_documented_clear_and_sync_fix_restores_broken_symlink_after_uv_cache_clean(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "workspace"
    cache_dir = tmp_path / "uv-cache"
    workspace.mkdir()
    shutil.copy(repo_root / "pyproject.toml", workspace / "pyproject.toml")
    shutil.copy(repo_root / "uv.lock", workspace / "uv.lock")
    shutil.copy(repo_root / "README.md", workspace / "README.md")
    shutil.copytree(repo_root / "litehive", workspace / "litehive")
    _copy_heru_dependency(repo_root, workspace)
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

    repair = _RUNNER.invoke(app, ["repair", "--workspace", str(workspace)], standalone_mode=False)
    assert repair.return_value == 1
    assert "uv venv --clear .venv && uv sync --extra dev" in repair.output

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
