"""Status diagnostics for broken workspace state."""

import argparse
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

import yaml

from litehive.config.paths import (
    workspace_daemon_lock_path,
    workspace_database_path,
    workspace_runner_lock_path,
    workspace_dir,
    workspace_logs_dir,
)
from litehive.domain.task import WorkspaceState
from litehive.main import _fast_status
from litehive.config.workspace import ensure_workspace
from litehive.state.persist import save_state

from tests.support.helpers import _cmd_status


def _run_fast_status(workspace: Path, capsys) -> tuple[int, str]:
    exit_code = _fast_status(["--workspace", str(workspace)])
    return exit_code, capsys.readouterr().out


def _run_full_status(workspace: Path, capsys) -> tuple[int, str]:
    exit_code = _cmd_status(argparse.Namespace(workspace=workspace, fast=False, full=True))
    return exit_code, capsys.readouterr().out


def test_status_reports_corrupt_workspace_dependencies_without_raising(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    config_file = workspace_dir(tmp_path) / "config.yaml"
    state_db = workspace_database_path(tmp_path)

    config_file.write_text("[", encoding="utf-8")
    state_db.write_text("not a sqlite database", encoding="utf-8")

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 1
    assert f"config: CORRUPT at {config_file} (line 1)" in output
    assert f"state: BROKEN at {state_db}" in output
    assert "restore the workspace database from backup" in output
    assert "health:" in output


def test_status_reports_invalid_merged_config_without_silent_defaulting(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    config_file = workspace_dir(tmp_path) / "config.yaml"
    config_file.write_text("poll_interval_seconds: not-a-number\n", encoding="utf-8")

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 1
    assert "config: INVALID merged config (" in output
    assert "poll_interval_seconds" in output
    assert "status is falling back to defaults" in output
    assert "health:" in output


def test_status_ignores_legacy_engine_fallbacks_config(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    config_file = workspace_dir(tmp_path) / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "engine_fallbacks": {
                    "codex": ["goz", "copilot"],
                    "goz": ["copilot"],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 0
    assert "config: INVALID merged config (" not in output
    assert "default_engine: codex" in output


def test_status_reports_invalid_engine_monitoring_schema(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    monitoring_file = workspace_dir(tmp_path) / "engine-monitoring.yaml"
    monitoring_file.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "claude": {
                        "engine": "claude",
                        "invocation_count": "many",
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 1
    assert f"engine_monitoring: INVALID at {monitoring_file} (engines.claude.invocation_count:" in output
    assert "restore engine usage details" in output
    assert "health:" in output


def test_status_reports_never_started_runner_without_lock(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 0
    assert "runner_status: never_started" in output


def test_status_reports_stale_runner_lock(tmp_path: Path, capsys, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    save_state(tmp_path, WorkspaceState(active_task_id="T-0001"))
    workspace_runner_lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    workspace_runner_lock_path(tmp_path).write_text(
        yaml.safe_dump(
            {
                "pid": 999999,
                "active_task_id": "T-0001",
                "started_at": "2026-04-12T00:00:00Z",
                "heartbeat_at": "2026-04-12T00:00:00Z",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("litehive.observability.status_diagnostics.runner_pid_is_alive", lambda pid: False)

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 1
    assert "runner_status: dead" in output
    assert "runner_state: STALE (no live pid for active_task_id=T-0001)" in output
    assert "litehive repair" in output
    assert "health:" in output


def test_full_status_reports_corrupt_runner_lock_without_raising(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    lock_path = workspace_runner_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("[", encoding="utf-8")

    exit_code, output = _run_full_status(tmp_path, capsys)

    assert exit_code == 1
    assert f"runner_state: CORRUPT at {lock_path} (line 1)" in output
    assert "Remove or rewrite the runner lock file" in output
    assert "health:" in output


def test_full_status_reports_invalid_runner_lock_schema(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    lock_path = workspace_runner_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("pid: nope\n", encoding="utf-8")

    exit_code, output = _run_full_status(tmp_path, capsys)

    assert exit_code == 1
    assert f"runner_state: INVALID at {lock_path} (pid:" in output
    assert "restart the runner or daemon" in output
    assert "health:" in output


def test_status_reports_stopped_runner_for_empty_lock_file(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    lock_path = workspace_runner_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{}\n", encoding="utf-8")

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 0
    assert "runner_status: stopped" in output


def test_status_reports_wedged_runner_heartbeat(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    stale_heartbeat = (datetime.now(UTC) - timedelta(minutes=11)).isoformat().replace("+00:00", "Z")
    workspace_runner_lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    workspace_runner_lock_path(tmp_path).write_text(
        yaml.safe_dump(
            {
                "pid": os.getpid(),
                "active_task_id": "T-0002",
                "started_at": stale_heartbeat,
                "heartbeat_at": stale_heartbeat,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 1
    assert "runner_state: WEDGED (heartbeat 11 min stale)" in output
    assert "restart the runner or daemon" in output


def test_status_reports_dead_daemon_pid(tmp_path: Path, capsys, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    daemon_lock = workspace_daemon_lock_path(tmp_path)
    daemon_lock.parent.mkdir(parents=True, exist_ok=True)
    daemon_lock.write_text(
        yaml.safe_dump(
            {
                "workspace": str(tmp_path.resolve()),
                "pid": 424242,
                "started_at": "2026-04-12T00:00:00Z",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("litehive.daemon.registry.pid_is_alive", lambda pid: False)
    monkeypatch.setattr("litehive.observability.status_diagnostics.pid_is_alive", lambda pid: False)

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 1
    assert "daemon_status: STOPPED (pid 424242 not alive)" in output
    assert "litehive start" in output


def test_status_reports_failed_last_cycle(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    log_dir = workspace_logs_dir(tmp_path) / "run-all" / "20260412T010203Z"
    log_dir.mkdir(parents=True, exist_ok=True)
    repair_log = log_dir / "0001-repair.log"
    repair_log.write_text("Traceback (most recent call last):\nboom\n", encoding="utf-8")

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 1
    assert f"last_cycle: FAILED at 20260412T010203Z, check {repair_log}" in output
    assert "inspect the traceback" in output


def test_status_reports_broken_heru_link(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "demo"
version = "0.1.0"

[tool.uv.sources]
heru = { path = "../heru", editable = true }
""".strip()
        + "\n",
        encoding="utf-8",
    )

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 1
    assert "heru_link: BROKEN (worktrees cannot resolve heru)" in output
    assert "uv sync" in output


def test_status_reports_origin_divergence_as_attention_required(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    ensure_workspace(tmp_path)
    save_state(tmp_path, WorkspaceState(pool_stop_reason="diverged_from_origin"))
    monkeypatch.setattr(
        "litehive.daemon.execution.check_origin_divergence",
        lambda workspace: (
            "local main (12345678) and origin/main (abcdef12) have diverged. "
            "Manual reconciliation required: run `git fetch origin main`, inspect "
            "`git log --oneline --left-right main...origin/main`, then rebase, reset, or merge "
            "before restarting the pool."
        ),
    )

    exit_code, output = _run_fast_status(tmp_path, capsys)

    assert exit_code == 1
    assert "pool_stop_reason: diverged_from_origin" in output
    assert "origin_divergence: !!! ATTENTION REQUIRED !!! local main (12345678) and origin/main (abcdef12) have diverged." in output
    assert "git fetch origin main" in output
    assert "git log --oneline --left-right main...origin/main" in output


def test_full_status_tolerates_missing_active_task_record(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    save_state(tmp_path, WorkspaceState(active_task_id="T-9999"))

    exit_code, output = _run_full_status(tmp_path, capsys)

    assert exit_code == 1
    assert "active_task_title:" not in output
    assert "runner_state: STALE (no live pid for active_task_id=T-9999)" in output
