import io
from pathlib import Path
import sys
from types import SimpleNamespace

from litehive.config.workspace import create_workspace
from litehive.daemon.execution import (
    _daemon_should_continue_for_stop_reason,
    _daemon_status_snapshot_for_workspace,
    _runner_is_live,
    run_daemon_loop,
)
from litehive.config.model import LitehiveConfig
from litehive.domain.common import RunnerExecutionStatus
from litehive.domain.pool import PoolStopReason
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import WorkspaceState
from litehive.state.persist import load_state_for_workspace, save_state_for_workspace
from litehive.state.records import create_task_for_workspace
from litehive.workspace import Workspace


def test_daemon_waits_for_live_runner_before_repair_or_run(tmp_path: Path, monkeypatch) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    active = create_task_for_workspace(workspace, title="Active runner task")
    queued = create_task_for_workspace(workspace, title="Queued follow-up task")

    state = load_state_for_workspace(workspace)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state_for_workspace(workspace, state)

    runner_checks = 0
    subprocess_calls: list[tuple[str, ...]] = []
    subprocess_cwds: list[Path] = []
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
        state = load_state_for_workspace(workspace)
        state.active_task_id = None
        save_state_for_workspace(workspace, state)

    def fake_run_logged_subprocess(command: list[str], **kwargs) -> int:
        subprocess_calls.append(tuple(command))
        subprocess_cwds.append(kwargs["cwd"])
        if "repair" not in command and "run" in command:
            state = load_state_for_workspace(workspace)
            state.queue = []
            state.active_task_id = None
            save_state_for_workspace(workspace, state)
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
    assert subprocess_calls[0][:3] == (sys.executable, "-m", "litehive.main")
    assert subprocess_cwds == [tmp_path.resolve()]
    assert any("run" in call for call in subprocess_calls)
    assert "runner already active: status=running pid=4242 active_task_id=" in stream.getvalue()


def test_daemon_processes_queued_tasks_sequentially(tmp_path: Path, monkeypatch) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    first = create_task_for_workspace(workspace, title="First queued task")
    second = create_task_for_workspace(workspace, title="Second queued task")

    subprocess_calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command: list[str], **kwargs) -> int:
        del kwargs
        subprocess_calls.append(tuple(command))
        state = load_state_for_workspace(workspace)
        state.active_task_id = None
        state.queue = state.queue[1:]
        save_state_for_workspace(workspace, state)
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
    assert load_state_for_workspace(workspace).queue == []


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

    snapshot = _daemon_status_snapshot_for_workspace(Workspace.from_path(tmp_path))

    assert snapshot.state.active_task_id == "T-0001"
    assert snapshot.state.queue == ["T-0002"]
    assert snapshot.state.pool_stop_reason == "attention_required"
    assert snapshot.text == "workspace: shared\nqueued_tasks: 1\n"
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
    assert _daemon_should_continue_for_stop_reason(PoolStopReason.QUEUE_EXHAUSTED) is True
    assert _daemon_should_continue_for_stop_reason(PoolStopReason.TASK_REQUEUED) is True
    assert _daemon_should_continue_for_stop_reason(PoolStopReason.ATTENTION_REQUIRED) is False


def test_daemon_stops_for_unknown_persisted_pool_stop_reason(tmp_path: Path, monkeypatch) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    create_task_for_workspace(workspace, title="Queued work")
    state = load_state_for_workspace(workspace)
    state.pool_stop_reason = "None"
    save_state_for_workspace(workspace, state)

    subprocess_calls = 0

    def fake_run_logged_subprocess(command: list[str], **kwargs) -> int:
        del command, kwargs
        nonlocal subprocess_calls
        subprocess_calls += 1
        return 0

    monkeypatch.setattr("litehive.daemon.execution.run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution.maybe_run_workspace_backup", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.check_origin_divergence", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")

    assert exit_code == 0
    assert subprocess_calls == 0
    assert "Runner stopped: None" in stream.getvalue()


def test_runner_is_live_uses_typed_runner_status() -> None:
    assert _runner_is_live(RunnerStatusState(status=RunnerExecutionStatus.RUNNING)) is True
    assert _runner_is_live(RunnerStatusState(status=RunnerExecutionStatus.LATE)) is True
    assert _runner_is_live(RunnerStatusState(status=RunnerExecutionStatus.IDLE)) is False
    assert _runner_is_live(RunnerStatusState(status=RunnerExecutionStatus.STALE)) is False
