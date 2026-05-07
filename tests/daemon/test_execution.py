import io
from pathlib import Path
import subprocess
from types import SimpleNamespace

from litehive.config.workspace import create_workspace
from litehive.daemon.execution import (
    _daemon_should_continue_for_stop_reason,
    _daemon_status_snapshot,
    check_origin_divergence,
    run_daemon_loop,
)
from litehive.config.model import LitehiveConfig
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import WorkspaceState
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task
from litehive.workspace import Workspace


def test_daemon_waits_for_live_runner_before_repair_or_run(tmp_path: Path, monkeypatch) -> None:
    create_workspace(tmp_path)
    active = create_task(tmp_path, title="Active runner task")
    queued = create_task(tmp_path, title="Queued follow-up task")

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    runner_checks = 0
    subprocess_calls: list[tuple[str, ...]] = []
    sleep_calls = 0

    def fake_runner_status(workspace: Workspace) -> RunnerStatusState:
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

    monkeypatch.setattr("litehive.daemon.execution.runner_status_for_workspace", fake_runner_status)
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
    assert len(subprocess_calls) == 1
    assert any("run" in call for call in subprocess_calls)
    assert "runner already active: status=running pid=4242 active_task_id=" in stream.getvalue()


def test_daemon_processes_queued_tasks_sequentially(tmp_path: Path, monkeypatch) -> None:
    create_workspace(tmp_path)
    first = create_task(tmp_path, title="First queued task")
    second = create_task(tmp_path, title="Second queued task")

    subprocess_calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command: list[str], **kwargs) -> int:
        del kwargs
        subprocess_calls.append(tuple(command))
        state = load_state(tmp_path)
        state.active_task_id = None
        state.queue = state.queue[1:]
        save_state(tmp_path, state)
        return 0

    monkeypatch.setattr("litehive.daemon.execution.run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution.maybe_run_workspace_backup", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.check_origin_divergence", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")

    assert exit_code == 0
    assert len(subprocess_calls) == 2
    assert all("run" in call for call in subprocess_calls)
    assert first.id in stream.getvalue()
    assert second.id in stream.getvalue()
    assert load_state(tmp_path).queue == []


def test_daemon_status_snapshot_uses_shared_status_collector(tmp_path: Path, monkeypatch) -> None:
    state = WorkspaceState(active_task_id="T-0001", queue=["T-0002"], pool_stop_reason="attention_required")
    shared_status = SimpleNamespace(
        config=LitehiveConfig(default_engine="claude"),
        state=state,
        runner=RunnerStatusState(),
        monitoring=None,
        issues=[],
        active_task_id="T-0001",
        active_task=None,
        queue_head="T-0002",
        waiting_lines=[],
        runner_state_label="never_started",
    )
    captured: dict[str, object] = {}

    def fake_collect(workspace, *, read_only: bool = False):
        captured["workspace"] = workspace
        captured["read_only"] = read_only
        return shared_status

    def fake_render(status, *, workspace: Path, mode: str, retry_on_label=None):
        captured["status"] = status
        captured["render_workspace"] = workspace
        captured["mode"] = mode
        captured["retry_on_label"] = retry_on_label
        return ["workspace: shared", "queued_tasks: 1"]

    monkeypatch.setattr("litehive.daemon.execution.collect_task_pipeline_status_for_workspace", fake_collect)
    monkeypatch.setattr("litehive.daemon.execution.render_task_pipeline_status_lines", fake_render)

    snapshot_state, text = _daemon_status_snapshot(tmp_path)

    assert snapshot_state["active_task_id"] == "T-0001"
    assert snapshot_state["queue"] == ["T-0002"]
    assert snapshot_state["pool_stop_reason"] == "attention_required"
    assert text == "workspace: shared\nqueued_tasks: 1\n"
    captured_workspace = captured.pop("workspace")
    assert captured == {
        "read_only": True,
        "status": shared_status,
        "render_workspace": tmp_path,
        "mode": "summary",
        "retry_on_label": None,
    }
    assert isinstance(captured_workspace, Workspace)
    assert captured_workspace.root == tmp_path.resolve()


def test_daemon_continues_only_for_absent_or_transient_stop_reasons() -> None:
    assert _daemon_should_continue_for_stop_reason(None) is True
    assert _daemon_should_continue_for_stop_reason("queue_exhausted") is True
    assert _daemon_should_continue_for_stop_reason("task_requeued") is True
    assert _daemon_should_continue_for_stop_reason("None") is False
    assert _daemon_should_continue_for_stop_reason("attention_required") is False


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
