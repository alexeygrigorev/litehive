import io
from pathlib import Path

from litehive.attention import append_attention_log, collect_operator_needed_state, waiting_for_you_lines
from litehive.config.paths import workspace_path
from litehive.config.workspace import ensure_workspace
from litehive.daemon.execution import run_daemon_loop
from litehive.main import dispatch_status
from litehive.sandbox.git_wrapper import main as git_wrapper_main
from litehive.state.persist import load_state, save_state, set_pool_stop_reason
from litehive.state.records import create_task, save_task
from litehive.domain.task import WorkspaceState


def test_status_surfaces_flagged_task_as_operator_needed(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Flagged work")
    task.status = "flagged"
    task.pipeline_status = "implementing"
    task.flag_reason = "needs operator review"
    save_task(tmp_path, task)

    exit_code = dispatch_status(["--workspace", str(tmp_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "operator_needed: true" in output
    assert "operator_needed_tasks: 1" in output
    assert "attention_items:" not in output

    lines = waiting_for_you_lines(tmp_path)
    assert f"operator_needed_task: {task.id} stage=implementing reason=needs operator review" in lines


def test_status_surfaces_runner_pool_stop_as_operator_needed(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    save_state(tmp_path, WorkspaceState(pool_stop_reason="diverged_from_origin"))

    exit_code = dispatch_status(["--workspace", str(tmp_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "operator_needed: true" in output
    assert "operator_needed_pool_stop_reason: diverged_from_origin" in output
    assert collect_operator_needed_state(tmp_path).pool_stop_reason == "diverged_from_origin"


def test_status_reports_no_operator_needed_when_workspace_is_clear(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)

    exit_code = dispatch_status(["--workspace", str(tmp_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "operator_needed: false" in output
    assert "operator_needed_tasks: 0" in output


def test_operator_log_is_runtime_diagnostic_not_queue_item(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    append_attention_log(tmp_path, "manual diagnostic")

    log_path = tmp_path / ".litehive" / "runtime" / "attention.log"
    assert "manual diagnostic" in log_path.read_text(encoding="utf-8")
    assert not (tmp_path / ".litehive" / "attention").exists()


def test_git_wrapper_block_logs_without_persisting_attention_queue(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)

    exit_code = git_wrapper_main(
        ["push", "--force", "origin", "main"],
        real_git_path="/usr/bin/git",
        workspace_root=str(tmp_path),
    )
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "blocked destructive git command" in err
    assert "merge-resolver git wrapper rejected `git push --force origin main`" in (
        tmp_path / ".litehive" / "runtime" / "attention.log"
    ).read_text(encoding="utf-8")
    assert not (tmp_path / ".litehive" / "attention").exists()


def test_runner_stops_before_running_tasks_when_operator_stop_reason_is_set(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued work")
    set_pool_stop_reason(tmp_path, "attention_required")

    calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command, **kwargs):
        calls.append(tuple(command))
        if any(arg == "run" for arg in command[2:]):
            raise AssertionError("pool should not start a task while operator intervention is needed")
        return 0

    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution.maybe_run_workspace_backup", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")
    output = stream.getvalue()

    assert exit_code == 0
    assert calls == []
    assert "Runner stopped: attention_required" in output


def test_runner_resumes_after_operator_stop_reason_is_cleared(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued work")
    set_pool_stop_reason(tmp_path, "attention_required")

    calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command, **kwargs):
        calls.append(tuple(command))
        if any(arg == "run" for arg in command[2:]):
            state = load_state(tmp_path)
            state.queue = []
            state.pool_stop_reason = None
            save_state(tmp_path, state)
        return 0

    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution.maybe_run_workspace_backup", lambda *args, **kwargs: None)

    first_stream = io.StringIO()
    first_exit_code = run_daemon_loop(tmp_path, output_stream=first_stream, session_dir=tmp_path / "logs-first")

    set_pool_stop_reason(tmp_path, None)
    second_stream = io.StringIO()
    second_exit_code = run_daemon_loop(tmp_path, output_stream=second_stream, session_dir=tmp_path / "logs-second")

    assert first_exit_code == 0
    assert "Runner stopped: attention_required" in first_stream.getvalue()
    assert second_exit_code == 0
    assert any("run" in command for command in calls)


def test_pool_halts_immediately_when_local_main_diverges_from_origin(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued work")

    calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command, **kwargs):
        calls.append(tuple(command))
        raise AssertionError("daemon should halt before repair or run when main diverges")

    monkeypatch.setattr(
        "litehive.daemon.execution.check_origin_divergence",
        lambda workspace: (
            "local main (12345678) and origin/main (abcdef12) have diverged. "
            "Manual reconciliation required: run `git fetch origin main`, inspect "
            "`git log --oneline --left-right main...origin/main`, then rebase, reset, or merge "
            "before restarting the pool."
        ),
    )
    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution.maybe_run_workspace_backup", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")
    output = stream.getvalue()

    assert exit_code == 0
    assert calls == []
    assert (
        "!!! ATTENTION REQUIRED !!! Local main has diverged from origin/main. Halting pool: diverged_from_origin"
        in output
    )
    assert "git fetch origin main" in output
    assert "git log --oneline --left-right main...origin/main" in output
    assert load_state(tmp_path).pool_stop_reason == "diverged_from_origin"
    assert workspace_path(tmp_path, "data.db").exists()
