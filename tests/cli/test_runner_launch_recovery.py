from dataclasses import dataclass
from pathlib import Path

from litehive.cli.runner import _run_once, daemon_worker
from litehive.config.workspace import ensure_workspace
from litehive.domain.recovery import TriggerEventKind
from litehive.recovery.detection import LaunchFailure, TaskLaunchFailure
from litehive.recovery.execution_recovery import LaunchRecoveryResult
from litehive.state.records import create_task, get_task
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.reports import load_task_thread, record_recovery_report


@dataclass(slots=True)
class _Result:
    task: object | None
    final_stage: str
    failed_reason: str | None = None
    failed_message: str | None = None


def _record_fake_recovery(root: Path, task, failure: LaunchFailure, *, fixed: bool) -> None:
    record_recovery_report(
        root,
        task,
        trigger_event_kind=TriggerEventKind.CRASH,
        origin_stage=implementation_entry_stage(task),
        summary=f"fake recovery {failure.context}",
        runnable_state="runnable" if fixed else "blocked",
        failure_classification=failure.context,
        blocker=None if fixed else failure.summary,
    )


def test_run_once_retries_uv_sync_failure_after_recovery(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover uv sync")
    prepare_calls = {"count": 0}
    recovery_calls = {"count": 0}

    def fake_prepare(root: Path, candidate) -> None:
        del root, candidate
        prepare_calls["count"] += 1
        if prepare_calls["count"] == 1:
            raise TaskLaunchFailure(
                context="venv_sync_failed",
                summary="uv sync failed in task worktree",
                diagnostics={"stderr": "broken editable path"},
            )

    def fake_recovery(root: Path, candidate, failure: LaunchFailure) -> LaunchRecoveryResult:
        recovery_calls["count"] += 1
        _record_fake_recovery(root, candidate, failure, fixed=True)
        return LaunchRecoveryResult(fixed=True, summary="fixed")

    monkeypatch.setattr("litehive.cli.runner.prepare_task_launch", fake_prepare)
    monkeypatch.setattr("litehive.cli.runner.attempt_launch_recovery", fake_recovery)
    monkeypatch.setattr("litehive.cli.runner.run_task", lambda *args, **kwargs: _Result(task=task, final_stage="done"))

    result = _run_once(tmp_path)

    assert result.exit_code == 0
    assert result.ran_task is True
    assert result.final_stage == "done"
    assert prepare_calls["count"] == 2
    assert recovery_calls["count"] == 1
    assert any(entry.role == "recovery" for entry in load_task_thread(tmp_path, task))


def test_run_once_flags_failed_launch_and_continues_to_next_task(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    broken = create_task(tmp_path, title="Broken launch")
    runnable = create_task(tmp_path, title="Runnable after skip")
    recovery_calls = {"count": 0}

    def fake_prepare(root: Path, candidate) -> None:
        del root
        if candidate.id == broken.id:
            raise TaskLaunchFailure(
                context="worktree_setup_failed",
                summary="git worktree add failed: stale ref",
            )

    def fake_recovery(root: Path, candidate, failure: LaunchFailure) -> LaunchRecoveryResult:
        recovery_calls["count"] += 1
        _record_fake_recovery(root, candidate, failure, fixed=False)
        return LaunchRecoveryResult(fixed=False, summary="no fix")

    monkeypatch.setattr("litehive.cli.runner.prepare_task_launch", fake_prepare)
    monkeypatch.setattr("litehive.cli.runner.attempt_launch_recovery", fake_recovery)
    monkeypatch.setattr(
        "litehive.cli.runner.run_task",
        lambda root, task, **kwargs: _Result(task=task, final_stage="done"),
    )

    result = _run_once(tmp_path)

    assert result.exit_code == 0
    assert result.ran_task is True
    assert result.final_stage == "done"
    assert recovery_calls["count"] == 1
    refreshed = get_task(tmp_path, broken.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.flag_reason == "recovery_failed"
    assert any(entry.role == "recovery" for entry in load_task_thread(tmp_path, broken))


def test_run_once_limits_launch_recovery_to_one_attempt(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Bounded launch recovery")
    prepare_calls = {"count": 0}
    recovery_calls = {"count": 0}

    def fake_prepare(root: Path, candidate) -> None:
        del root, candidate
        prepare_calls["count"] += 1
        raise TaskLaunchFailure(
            context="pre_stage_setup_failed",
            summary="corrupt runner lock",
        )

    def fake_recovery(root: Path, candidate, failure: LaunchFailure) -> LaunchRecoveryResult:
        recovery_calls["count"] += 1
        _record_fake_recovery(root, candidate, failure, fixed=True)
        return LaunchRecoveryResult(fixed=True, summary="fixed once")

    monkeypatch.setattr("litehive.cli.runner.prepare_task_launch", fake_prepare)
    monkeypatch.setattr("litehive.cli.runner.attempt_launch_recovery", fake_recovery)

    result = _run_once(tmp_path)

    assert result.exit_code == 0
    assert result.ran_task is False
    assert prepare_calls["count"] == 2
    assert recovery_calls["count"] == 1
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.flag_reason == "recovery_failed"


def test_run_once_flags_corrupt_queued_task_yaml_and_continues(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    broken = create_task(tmp_path, title="Broken yaml", acceptance_criteria=["ok"])
    runnable = create_task(tmp_path, title="Runnable after bad")
    broken_task_dir = tmp_path / ".litehive" / "tasks" / f"{broken.id}-{broken.slug}"
    (broken_task_dir / "task.yaml").write_text(
        f"id: {broken.id}\nacceptance_criteria:\n  - broken: colon\n",
        encoding="utf-8",
    )

    run_calls: list[str] = []

    def fake_run_task(root: Path, task, **kwargs) -> _Result:
        del root, kwargs
        run_calls.append(task.id)
        return _Result(task=task, final_stage="done")

    monkeypatch.setattr("litehive.cli.runner.run_task", fake_run_task)

    result = _run_once(tmp_path)

    assert result.exit_code == 0
    assert result.ran_task is True
    assert run_calls == [runnable.id]
    refreshed_broken = get_task(tmp_path, broken.id)
    assert refreshed_broken is not None
    assert refreshed_broken.status == "flagged"
    assert refreshed_broken.flag_reason == "recovery_failed"
    assert any(entry.role == "recovery" for entry in load_task_thread(tmp_path, refreshed_broken))
    backups = sorted(broken_task_dir.glob("task.yaml.corrupt-*"))
    assert backups


def test_run_once_attempts_corrupt_task_yaml_recovery_once(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    broken = create_task(tmp_path, title="Broken yaml", acceptance_criteria=["ok"])
    runnable = create_task(tmp_path, title="Runnable after bad")
    broken_task_dir = tmp_path / ".litehive" / "tasks" / f"{broken.id}-{broken.slug}"
    (broken_task_dir / "task.yaml").write_text(
        f"id: {broken.id}\nacceptance_criteria:\n  - broken: colon\n",
        encoding="utf-8",
    )

    recovery_calls: list[tuple[str, str]] = []
    run_calls: list[str] = []

    def fake_recovery(root: Path, candidate, failure: LaunchFailure) -> LaunchRecoveryResult:
        del root
        recovery_calls.append((candidate.id, failure.context))
        return LaunchRecoveryResult(fixed=False, summary="no fix")

    def fake_run_task(root: Path, task, **kwargs) -> _Result:
        del root, kwargs
        run_calls.append(task.id)
        return _Result(task=task, final_stage="done")

    monkeypatch.setattr("litehive.cli.runner.attempt_launch_recovery", fake_recovery)
    monkeypatch.setattr("litehive.cli.runner.run_task", fake_run_task)

    result = _run_once(tmp_path)

    assert result.exit_code == 0
    assert result.ran_task is True
    assert recovery_calls == [(broken.id, "pre_stage_setup_failed")]
    assert run_calls == [runnable.id]
    refreshed_broken = get_task(tmp_path, broken.id)
    assert refreshed_broken is not None
    assert refreshed_broken.status == "flagged"
    assert refreshed_broken.flag_reason == "recovery_failed"
    assert any(entry.role == "recovery" for entry in load_task_thread(tmp_path, refreshed_broken))


def test_daemon_worker_defers_preflight_to_run_daemon_loop(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fail_if_called(*args, **kwargs) -> None:
        del args, kwargs
        calls.append("preflight")
        raise AssertionError("daemon_worker should defer bootstrap to run_daemon_loop")

    monkeypatch.setattr("litehive.cli.runner.ensure_workspace", fail_if_called)
    monkeypatch.setattr("litehive.cli.runner.apply_pending_migrations", fail_if_called)
    monkeypatch.setattr("litehive.cli.runner.run_daemon_loop", lambda workspace, output_stream=None: 7)

    result = daemon_worker(tmp_path)

    assert result == 7
    assert calls == []
