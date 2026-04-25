import io
from pathlib import Path
import subprocess

from litehive.config.workspace import ensure_workspace
from litehive.daemon.execution import check_origin_divergence, run_daemon_loop
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
    monkeypatch.setattr("litehive.daemon.execution.sleep_with_stop", fake_sleep)
    monkeypatch.setattr("litehive.daemon.execution.run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution.maybe_run_workspace_backup", lambda *args, **kwargs: None)
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


def test_check_origin_divergence_compares_main_even_when_head_is_elsewhere(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    workspace = tmp_path / "workspace"
    remote_clone = tmp_path / "remote-clone"

    subprocess.run(["git", "init", "--bare", str(origin)], check=True)
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=workspace, check=True)

    readme = workspace / "README.md"
    readme.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=workspace, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=workspace, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=workspace, check=True)

    readme.write_text("local\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "local"], cwd=workspace, check=True)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=workspace, check=True)

    subprocess.run(["git", "clone", str(origin), str(remote_clone)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=remote_clone, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=remote_clone, check=True)
    remote_readme = remote_clone / "README.md"
    remote_readme.write_text("remote\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "remote"], cwd=remote_clone, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=remote_clone, check=True)

    message = check_origin_divergence(workspace)

    assert message is not None
    assert "local main (" in message
    assert "origin/main (" in message
    assert "Manual reconciliation required" in message
