from dataclasses import dataclass
from pathlib import Path

import pytest

from litehive.cli.runner import daemon_worker, run_once
from litehive.config.workspace import ensure_workspace
from litehive.domain.recovery import TriggerEventKind
from litehive.recovery.detection import LaunchFailure, TaskLaunchFailure
from litehive.recovery.execution_recovery import LaunchRecoveryResult
from litehive.state.records import create_task, get_task
from litehive.tasks.activity import load_task_activity
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.reports import load_recovery_reports, record_recovery_report


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


def _assert_flagged_with_recovery_activity(root: Path, task_id: str) -> None:
    refreshed = get_task(root, task_id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.flag_reason == "recovery_failed"
    assert any(entry.role == "recovery" for entry in load_task_activity(root, refreshed))


def test_record_recovery_report_uses_sqlite_storage(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery storage")

    record_recovery_report(
        tmp_path,
        task,
        trigger_event_kind=TriggerEventKind.CRASH,
        origin_stage="implementing",
        summary="captured crash",
        runnable_state="blocked",
        failure_classification="crash",
        blocker="boom",
    )

    reports = load_recovery_reports(tmp_path, task)
    assert len(reports) == 1
    assert reports[0].summary == "captured crash"
    assert list((tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "recovery").glob("*.yaml")) == []


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

    result = run_once(tmp_path)

    assert result.exit_code == 0
    assert result.ran_task is True
    assert result.final_stage == "done"
    assert prepare_calls["count"] == 2
    assert recovery_calls["count"] == 1
    assert any(entry.role == "recovery" for entry in load_task_activity(tmp_path, task))


@pytest.mark.parametrize(
    ("title", "context", "summary", "fixed", "expect_ran_task"),
    [
        ("Broken launch", "worktree_setup_failed", "git worktree add failed: stale ref", False, True),
        ("Bounded launch recovery", "pre_stage_setup_failed", "corrupt runner lock", True, False),
    ],
)
def test_run_once_handles_unrecoverable_launch_failure(
    tmp_path: Path,
    monkeypatch,
    title: str,
    context: str,
    summary: str,
    fixed: bool,
    expect_ran_task: bool,
) -> None:
    ensure_workspace(tmp_path)
    broken = create_task(tmp_path, title=title)
    runnable = None if not expect_ran_task else create_task(tmp_path, title="Runnable after skip")
    prepare_calls = {"count": 0}
    recovery_calls = {"count": 0}

    def fake_prepare(root: Path, candidate) -> None:
        del root
        prepare_calls["count"] += 1
        if expect_ran_task and candidate.id != broken.id:
            return
        raise TaskLaunchFailure(context=context, summary=summary)

    def fake_recovery(root: Path, candidate, failure: LaunchFailure) -> LaunchRecoveryResult:
        recovery_calls["count"] += 1
        _record_fake_recovery(root, candidate, failure, fixed=fixed)
        return LaunchRecoveryResult(fixed=fixed, summary="fixed once" if fixed else "no fix")

    monkeypatch.setattr("litehive.cli.runner.prepare_task_launch", fake_prepare)
    monkeypatch.setattr("litehive.cli.runner.attempt_launch_recovery", fake_recovery)
    monkeypatch.setattr(
        "litehive.cli.runner.run_task",
        lambda root, task, **kwargs: _Result(task=task, final_stage="done"),
    )

    result = run_once(tmp_path)

    assert result.exit_code == 0
    assert result.ran_task is expect_ran_task
    assert prepare_calls["count"] == 2
    assert recovery_calls["count"] == 1
    _assert_flagged_with_recovery_activity(tmp_path, broken.id)
    if expect_ran_task and runnable is not None:
        assert result.final_stage == "done"
    else:
        assert result.final_stage is None


def test_daemon_worker_defers_preflight_to_run_daemon_loop(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fail_if_called(*args, **kwargs) -> None:
        del args, kwargs
        calls.append("preflight")
        raise AssertionError("daemon_worker should defer bootstrap to run_daemon_loop")

    monkeypatch.setattr("litehive.cli.runner.ensure_workspace", fail_if_called)
    monkeypatch.setattr("litehive.cli.runner.apply_pending_migrations", fail_if_called)
    monkeypatch.setattr("litehive.cli.runner.run_daemon_loop", lambda workspace, output_stream=None: 7)

    assert daemon_worker(tmp_path) == 7
    assert calls == []
