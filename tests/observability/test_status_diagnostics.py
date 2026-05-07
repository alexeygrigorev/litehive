"""Status diagnostics for broken workspace state."""

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import warnings

import yaml

from heru import get_engine
from heru.base import CLIExecutionResult
from litehive.db.schema import connect_workspace_db
from litehive.config.paths import litehive_root, workspace_path
from litehive.config.workspace import create_workspace
from litehive.config.workspace_files import workspace_dir
from litehive.domain.recovery import FailureFingerprint, RecoveryTrigger, TriggerEventKind
from litehive.domain.task import WorkspaceState
from litehive.lifecycle.persistence import SqlitePersistence, TaskState
from litehive.lifecycle.types import FailedReason, PipelineMode
from litehive.main import dispatch_status
from litehive.observability.engine_monitoring import record_engine_execution
from litehive.observability.status_diagnostics import (
    _load_runner_status_for_status,
    collect_operational_status_snapshot_for_workspace,
    collect_status_snapshot_for_workspace,
)
from litehive.state.records import create_task, save_task
from litehive.state.persist import save_state
from litehive.workspace import Workspace

from tests.support.helpers import _cmd_status
from litehive.domain.common import PipelineState, PipelineStatus, TaskStatus


def _run_dispatch_status(workspace: Path, capsys) -> tuple[int, str]:
    exit_code = dispatch_status(["--workspace", str(workspace)])
    return exit_code, capsys.readouterr().out


def _run_full_status(workspace: Path, capsys) -> tuple[int, str]:
    exit_code = _cmd_status(argparse.Namespace(workspace=workspace, fast=False, full=True))
    return exit_code, capsys.readouterr().out


def test_status_snapshot_does_not_bootstrap_missing_database(tmp_path: Path) -> None:
    workspace_dir(tmp_path).mkdir(parents=True)
    (workspace_dir(tmp_path) / "config.yaml").write_text("{}", encoding="utf-8")
    state_db = workspace_path(tmp_path, "data.db")

    snapshot = collect_status_snapshot_for_workspace(Workspace.from_path(tmp_path))

    assert snapshot.state.queue == []
    assert not state_db.exists()


def test_operational_status_snapshot_does_not_run_doctor_style_probes(tmp_path: Path, monkeypatch) -> None:
    create_workspace(tmp_path)
    monkeypatch.setattr(
        "litehive.observability.status_diagnostics._probe_daemon_status",
        lambda root: (_ for _ in ()).throw(AssertionError("daemon probe should not run")),
    )
    monkeypatch.setattr(
        "litehive.observability.status_diagnostics._probe_last_cycle",
        lambda root: (_ for _ in ()).throw(AssertionError("last-cycle probe should not run")),
    )
    monkeypatch.setattr(
        "litehive.observability.status_diagnostics._probe_heru_link",
        lambda root: (_ for _ in ()).throw(AssertionError("source checkout probe should not run")),
    )
    monkeypatch.setattr(
        "litehive.observability.status_diagnostics._probe_task_index_references",
        lambda root, state, state_issues: (_ for _ in ()).throw(AssertionError("task index probe should not run")),
    )

    snapshot = collect_operational_status_snapshot_for_workspace(Workspace.from_path(tmp_path))

    assert snapshot.state.queue == []


def test_status_reports_corrupt_workspace_dependencies_without_raising(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    config_file = workspace_dir(tmp_path) / "config.yaml"
    state_db = workspace_path(tmp_path, "data.db")

    config_file.write_text("[", encoding="utf-8")
    state_db.write_text("not a sqlite database", encoding="utf-8")

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 1
    assert f"config: CORRUPT at {config_file} (line 1)" in output
    assert f"state: BROKEN at {state_db}" in output
    assert "restore the workspace database from backup" not in output
    assert "health:" in output


def test_status_reports_corrupt_workspace_registry_without_raising(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    create_workspace(tmp_path)
    registry_path = litehive_root() / "workspaces.db"
    registry_path.write_text("not a sqlite database", encoding="utf-8")

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert f"registry: BROKEN at {registry_path}" not in output
    assert "global workspace registry database" not in output
    assert "runner_status:" in output
    assert "health:" not in output


def test_status_reports_invalid_merged_config_without_silent_defaulting(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    config_file = workspace_dir(tmp_path) / "config.yaml"
    config_file.write_text("poll_interval_seconds: not-a-number\n", encoding="utf-8")

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 1
    assert "config: INVALID merged config (" in output
    assert "poll_interval_seconds" in output
    assert "status is rendering with valid config fields only" not in output
    assert "health:" in output


def test_status_reports_non_mapping_config_without_silent_defaulting(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    config_file = workspace_dir(tmp_path) / "config.yaml"
    config_file.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 1
    assert f"config: INVALID at {config_file} (expected YAML mapping)" in output
    assert "Fix the YAML syntax or remove the file" not in output
    assert "health:" in output


def test_status_preserves_valid_config_fields_when_reporting_invalid_config(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    config_file = workspace_dir(tmp_path) / "config.yaml"
    config_file.write_text(
        "default_engine: claude\npoll_interval_seconds: not-a-number\n",
        encoding="utf-8",
    )

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 1
    assert "default_engine: claude" in output
    assert "config: INVALID merged config (" in output
    assert "poll_interval_seconds" in output


def test_status_reports_legacy_engine_fallbacks_config_error(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
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

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 1
    assert "config: INVALID merged config (" in output
    assert "unknown config key 'engine_fallbacks'" in output


def test_status_ignores_legacy_engine_monitoring_yaml_and_renders_db_data(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    record_engine_execution(
        Workspace.from_path(tmp_path),
        task_id="T-0001",
        engine_name="codex",
        adapter=get_engine("codex"),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=tmp_path,
            exit_code=0,
            stdout="",
            stderr="",
        ),
        failure_kind=None,
        failure_reason=None,
    )
    monitoring_file = workspace_dir(tmp_path) / "engine-monitoring.yaml"
    monitoring_file.write_text("[", encoding="utf-8")

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert f"CORRUPT at {monitoring_file}" not in output
    assert "engine_available: codex status=available default=yes" in output
    assert "engine_monitoring:" not in output


def test_status_reports_never_started_runner_without_lock(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert "runner_status: never_started" in output


def test_status_reports_stale_runner_lock(tmp_path: Path, capsys, monkeypatch) -> None:
    create_workspace(tmp_path)
    save_state(tmp_path, WorkspaceState(active_task_id="T-0001"))
    workspace_path(tmp_path, "runtime", ".runner.lock").parent.mkdir(parents=True, exist_ok=True)
    workspace_path(tmp_path, "runtime", ".runner.lock").write_text(
        json.dumps(
            {
                "pid": 999999,
                "active_task_id": "T-0001",
                "started_at": "2026-04-12T00:00:00Z",
                "heartbeat_at": "2026-04-12T00:00:00Z",
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("litehive.observability.status_loaders.runner_pid_is_alive", lambda pid: False)
    monkeypatch.setattr("litehive.observability.status_probes.runner_pid_is_alive", lambda pid: False)

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 1
    assert "runner_status: dead" in output
    assert "runner_state: STALE (no live pid for active_task_id=T-0001)" in output
    assert "litehive repair" not in output
    assert "health:" in output


def test_full_status_reports_corrupt_runner_lock_without_raising(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    lock_path = workspace_path(tmp_path, "runtime", ".runner.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("[", encoding="utf-8")

    exit_code, output = _run_full_status(tmp_path, capsys)

    assert exit_code == 1
    assert f"runner_state: CORRUPT at {lock_path} (line 1)" in output
    assert "Remove or rewrite the runner lock file" in output
    assert "health:" in output


def test_full_status_reports_invalid_runner_lock_schema(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    lock_path = workspace_path(tmp_path, "runtime", ".runner.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": "nope"}), encoding="utf-8")

    exit_code, output = _run_full_status(tmp_path, capsys)

    assert exit_code == 1
    assert f"runner_state: INVALID at {lock_path} (pid:" in output
    assert "restart the runner or daemon" in output
    assert "health:" in output


def test_full_status_reports_non_mapping_runner_lock_without_empty_default(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    lock_path = workspace_path(tmp_path, "runtime", ".runner.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("[]", encoding="utf-8")

    exit_code, output = _run_full_status(tmp_path, capsys)

    assert exit_code == 1
    assert f"runner_state: INVALID at {lock_path} (expected JSON object)" in output
    assert "Remove or rewrite the runner lock file" in output
    assert "health:" in output


def test_status_reports_stopped_runner_for_empty_lock_file(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    lock_path = workspace_path(tmp_path, "runtime", ".runner.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{}\n", encoding="utf-8")

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert "runner_status: stopped" in output


def test_status_reports_queued_task_missing_from_sqlite_index(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Still queued")
    save_state(tmp_path, WorkspaceState(queue=[task.id]))
    with connect_workspace_db(tmp_path) as connection:
        connection.execute("DELETE FROM task_state WHERE task_id = ?", (task.id,))
        connection.execute("DELETE FROM task_intent WHERE task_id = ?", (task.id,))
        connection.commit()

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert "queued_tasks: 1" in output
    assert f"queue_head: {task.id}" in output
    assert "task_index:" not in output
    assert "restore/reconcile the workspace database" not in output
    assert "health:" not in output


def test_runner_status_diagnostic_copies_serialize_without_pydantic_warnings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    create_workspace(tmp_path)
    lock_path = workspace_path(tmp_path, "runtime", ".runner.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    for pid_is_alive, expected_status in ((True, "running"), (False, "stale")):
        lock_path.write_text(
            json.dumps(
                {
                    "pid": 4242,
                    "active_task_id": "T-0002",
                    "started_at": "2026-04-12T00:00:00Z",
                    "heartbeat_at": "2026-04-12T00:00:00Z",
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "litehive.observability.status_loaders.runner_pid_is_alive",
            lambda pid, alive=pid_is_alive: alive,
        )

        status, issue = _load_runner_status_for_status(tmp_path)

        assert issue is None
        with warnings.catch_warnings():
            warnings.filterwarnings("error", message="Pydantic serializer warnings", category=UserWarning)
            payload = status.model_dump(mode="json")
        assert payload["status"] == expected_status


def test_status_reports_wedged_runner_heartbeat(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    stale_heartbeat = (datetime.now(UTC) - timedelta(minutes=11)).isoformat().replace("+00:00", "Z")
    workspace_path(tmp_path, "runtime", ".runner.lock").parent.mkdir(parents=True, exist_ok=True)
    workspace_path(tmp_path, "runtime", ".runner.lock").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "active_task_id": "T-0002",
                "started_at": stale_heartbeat,
                "heartbeat_at": stale_heartbeat,
            },
        ),
        encoding="utf-8",
    )

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 1
    assert "runner_state: WEDGED (heartbeat 11 min stale)" in output
    assert "restart the runner or daemon" not in output


def test_status_reports_dead_daemon_pid(tmp_path: Path, capsys, monkeypatch) -> None:
    create_workspace(tmp_path)
    daemon_lock = workspace_path(tmp_path, "runtime", ".daemon.lock")
    daemon_lock.parent.mkdir(parents=True, exist_ok=True)
    daemon_lock.write_text(
        json.dumps(
            {
                "workspace": str(tmp_path.resolve()),
                "pid": 424242,
                "started_at": "2026-04-12T00:00:00Z",
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("litehive.daemon.registry.runner_pid_is_alive", lambda pid: False)
    monkeypatch.setattr("litehive.observability.status_probes.runner_pid_is_alive", lambda pid: False)

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert "daemon_status:" not in output
    assert "litehive start" not in output


def test_status_reports_failed_last_cycle(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    log_dir = workspace_path(tmp_path, "logs", "run-all", "20260412T010203Z")
    log_dir.mkdir(parents=True, exist_ok=True)
    repair_log = log_dir / "0001-repair.log"
    repair_log.write_text("Traceback (most recent call last):\nboom\n", encoding="utf-8")

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert "last_cycle:" not in output
    assert str(repair_log) not in output
    assert "inspect the traceback" not in output


def test_status_reports_broken_heru_link(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
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

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert "heru_link:" not in output
    assert "uv sync" not in output


def test_status_reports_origin_divergence_as_attention_required(tmp_path: Path, capsys, monkeypatch) -> None:
    create_workspace(tmp_path)
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

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert "pool_stop_reason: diverged_from_origin" in output
    assert "origin_divergence:" not in output
    assert "git fetch origin main" not in output
    assert "git log --oneline --left-right main...origin/main" not in output


def test_full_status_reports_consecutive_task_failures_as_critical(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    save_state(
        tmp_path,
        WorkspaceState(
            pool_stop_reason="consecutive_task_failures",
            consecutive_task_failures=3,
        ),
    )

    exit_code, output = _run_full_status(tmp_path, capsys)

    assert exit_code == 1
    assert "pool_stop_reason: consecutive_task_failures" in output
    assert "critical_status: CRITICAL: pool stopped after 3 consecutive task failures" in output
    assert "health: 1 broken, 0 warning" in output


def test_status_omits_recovery_failure_repair_guidance_from_default_path(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery failed task")
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.FLAGGED
    task.flag_reason = "recovery_failed"
    task.runtime.pipeline.last_outcome.stage = PipelineState.IMPLEMENTING
    task.runtime.pipeline.last_outcome.reason = "recovery crashed while repairing the task"
    save_task(tmp_path, task)

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert "recovery_failure:" not in output
    assert "recovery crashed while repairing the task" not in output
    assert "litehive task evidence T-0001" not in output
    assert "health:" not in output


def test_full_status_reports_terminal_recovery_failure_from_lifecycle_state(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Terminal lifecycle recovery failure")
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.FLAGGED
    task.flag_reason = "needs_operator_review"
    save_task(tmp_path, task)
    SqlitePersistence(Workspace.from_path(tmp_path)).save(
        TaskState(
            task_id=task.id,
            stage=PipelineState.FAILED,
            pipeline_mode=PipelineMode.FULL,
            active_recovery_trigger=RecoveryTrigger(
                origin_stage="implementing",
                trigger_event_kind=TriggerEventKind.CRASH,
                failure_fingerprint=FailureFingerprint(
                    fingerprint="RuntimeError:boom",
                    classification="RuntimeError",
                ),
                source="runner",
                message="recovery crashed before selecting a resume stage",
            ),
            failed_reason=FailedReason.RECOVERY_MISSING_TARGET_STAGE,
            failed_message="recovery reported success but did not provide a target stage",
        )
    )

    exit_code, output = _run_full_status(tmp_path, capsys)

    assert exit_code == 1
    assert (
        "recovery_failure: Task T-0001 has recovery failure (recovery_missing_target_stage) at `implementing`" in output
    )
    assert "recovery reported success but did not provide a target stage" in output
    assert "litehive task evidence T-0001" in output
    assert "health: 1 broken, 0 warning" in output


def test_status_reports_queued_backlog_task_with_resumable_runtime_stage(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Backlog damaged task")
    task.status = TaskStatus.QUEUED
    task.pipeline_status = PipelineStatus.BACKLOG
    task.runtime.pipeline.current_stage.stage = PipelineState.TESTING
    task.runtime.pipeline.current_stage.status = "idle"
    save_task(tmp_path, task)

    exit_code, output = _run_full_status(tmp_path, capsys)

    assert exit_code == 1
    assert "backlog_damage: Task T-0001 is queued/backlog but runtime says resume from `testing`" in output
    assert "litehive repair" in output
    assert "litehive queue resume T-0001" in output
    assert "health: 1 broken, 0 warning" in output


def test_status_omits_backlog_runtime_stage_repair_guidance_from_default_path(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Backlog damaged task missing from queue")
    task.status = TaskStatus.QUEUED
    task.pipeline_status = PipelineStatus.BACKLOG
    task.runtime.pipeline.current_stage.stage = PipelineState.TESTING
    task.runtime.pipeline.current_stage.status = "idle"
    save_task(tmp_path, task)
    save_state(tmp_path, WorkspaceState(queue=[]))

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert "queued_tasks: 0" in output
    assert "backlog_damage:" not in output
    assert "missing from WorkspaceState.queue" not in output
    assert "litehive queue resume T-0001" not in output
    assert "health:" not in output


def test_status_omits_queued_stage_normalization_warning_from_default_path(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Stale queued stage")
    task.status = TaskStatus.QUEUED
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    task.runtime.pipeline.current_stage.stage = None
    task.runtime.pipeline.current_stage.status = "idle"
    save_task(tmp_path, task)

    exit_code, output = _run_dispatch_status(tmp_path, capsys)

    assert exit_code == 0
    assert "backlog_damage:" not in output
    assert "the queue will normalize it back to backlog" not in output
    assert "health:" not in output


def test_full_status_tolerates_missing_active_task_record(tmp_path: Path, capsys) -> None:
    create_workspace(tmp_path)
    save_state(tmp_path, WorkspaceState(active_task_id="T-9999"))

    exit_code, output = _run_full_status(tmp_path, capsys)

    assert exit_code == 1
    assert "active_task_title:" not in output
    assert "runner_state: STALE (no live pid for active_task_id=T-9999)" in output
