import io
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.daemon.execution import run_daemon_loop
from litehive.domain.runtime import RunnerStatusState
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task


def test_daemon_waits_for_live_runner_before_repair_or_run(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active runner task")
    queued = create_task(tmp_path, title="Queued follow-up task")

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    runner_checks = 0
    subprocess_calls: list[tuple[str, ...]] = []
    sleep_calls = 0

    def fake_runner_status(workspace: Path) -> RunnerStatusState:
        del workspace
        nonlocal runner_checks
        runner_checks += 1
        if runner_checks == 1:
            return RunnerStatusState(
                status="running",
                pid=4242,
                active_task_id=active.id,
                heartbeat_at="2026-04-22T04:16:07+00:00",
            )
        return RunnerStatusState()

    def fake_sleep(seconds: float, *, stop_requested_fn) -> None:
        del stop_requested_fn
        nonlocal sleep_calls
        sleep_calls += 1
        assert seconds == 1.0
        state = load_state(tmp_path)
        state.active_task_id = None
        save_state(tmp_path, state)

    def fake_run_logged_subprocess(command: list[str], **kwargs) -> int:
        del kwargs
        subprocess_calls.append(tuple(command))
        if "repair" not in command and "run" in command:
            state = load_state(tmp_path)
            state.queue = []
            state.active_task_id = None
            save_state(tmp_path, state)
        return 0

    monkeypatch.setattr("litehive.daemon.execution.runner_status", fake_runner_status)
    monkeypatch.setattr("litehive.daemon.execution._sleep_with_stop", fake_sleep)
    monkeypatch.setattr("litehive.daemon.execution._run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution._maybe_run_workspace_backup", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.check_origin_divergence", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")

    assert exit_code == 0
    assert sleep_calls == 1
    assert runner_checks >= 2
    assert len(subprocess_calls) == 2
    assert any("repair" in call for call in subprocess_calls)
    assert any("run" in call for call in subprocess_calls)
    assert "runner already active: status=running pid=4242 active_task_id=" in stream.getvalue()
