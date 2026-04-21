import io
from pathlib import Path

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.daemon.execution import run_daemon_loop
from litehive.recovery.detection import LaunchFailure
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task
from litehive.tasks.reports import load_task_thread


def test_daemon_loop_recovers_corrupt_workspaces_yaml_before_cycle_start(tmp_path: Path, monkeypatch) -> None:
    data_home = tmp_path / "data-home"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Queue head")

    registry_path = data_home / "litehive" / "workspaces.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text("broken: [\n", encoding="utf-8")

    calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command, **kwargs):
        calls.append(tuple(command))
        if "run" in command:
            state = load_state(tmp_path)
            state.queue = []
            state.active_task_id = None
            save_state(tmp_path, state)
        return 0

    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution._run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution._maybe_run_workspace_backup", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")

    assert exit_code == 0
    assert any("repair" in command for command in calls)
    assert any("run" in command for command in calls)
    backups = sorted(registry_path.parent.glob("workspaces.yaml.corrupt-*"))
    assert backups
    assert registry_path.exists()
    assert registry_path.read_text(encoding="utf-8").strip().startswith("- ")
    thread = load_task_thread(tmp_path, task)
    assert any(entry.role == "recovery" for entry in thread)
    assert "launch recovery fixed: cycle_start_failed" in stream.getvalue()


def test_daemon_loop_bounds_cycle_start_recovery_to_one_attempt(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queue head")
    calls: list[str] = []
    failures = [
        LaunchFailure(context="cycle_start_failed", summary="startup broken"),
        LaunchFailure(context="cycle_start_failed", summary="startup broken"),
    ]

    monkeypatch.setattr(
        "litehive.daemon.execution.detect_cycle_start_failure",
        lambda workspace: failures.pop(0) if failures else None,
    )

    def fake_recover(workspace, failure, *, output_stream):
        del workspace, output_stream
        calls.append(failure.context)
        return False

    monkeypatch.setattr("litehive.daemon.execution._recover_cycle_start_failure", fake_recover)
    monkeypatch.setattr(
        "litehive.daemon.execution.flag_task_after_failed_launch_recovery",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(55)),
    )
    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution._ensure_workspace_venvs_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution._maybe_run_workspace_backup", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.check_origin_divergence", lambda *args, **kwargs: None)

    with pytest.raises(SystemExit) as excinfo:
        run_daemon_loop(tmp_path, output_stream=io.StringIO(), session_dir=tmp_path / "logs")

    assert excinfo.value.code == 55
    assert calls == ["cycle_start_failed"]
