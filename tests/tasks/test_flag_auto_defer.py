"""Tests for flag_count lifetime counter, auto-defer after 3 flags, and requeue --force."""

import argparse
from pathlib import Path

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.state.persist import load_state
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.runtime import finish_task_run_transition
from litehive.tasks.status import requeue_task

from tests.support.helpers import _cmd_requeue_task


def _flag_task(root: Path, task_id: str) -> None:
    """Set a task to flagged and persist via finish_task_run_transition."""
    task = get_task(root, task_id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "implementing"
    finish_task_run_transition(root, task, "flagged")


def test_flag_count_increments_on_each_flagged_transition(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Flaky task")

    _flag_task(tmp_path, task.id)
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.flag_count == 1

    # Requeue and flag again
    requeue_task(tmp_path, task.id, force=True)
    _flag_task(tmp_path, task.id)
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.flag_count == 2


def test_auto_defer_after_three_flags(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Repeatedly failing task")

    # Flag 1
    _flag_task(tmp_path, task.id)
    t = get_task(tmp_path, task.id)
    assert t is not None
    assert t.status == "flagged"
    assert t.flag_count == 1

    # Flag 2
    requeue_task(tmp_path, task.id, force=True)
    _flag_task(tmp_path, task.id)
    t = get_task(tmp_path, task.id)
    assert t is not None
    assert t.status == "flagged"
    assert t.flag_count == 2

    # Flag 3 -> auto-defer
    requeue_task(tmp_path, task.id, force=True)
    _flag_task(tmp_path, task.id)
    t = get_task(tmp_path, task.id)
    assert t is not None
    assert t.status == "deferred"
    assert t.flag_count == 3
    assert t.flag_reason == "flagged 3 times - needs human review"

    # Task should be out of the queue
    state = load_state(tmp_path)
    assert task.id not in state.queue


def test_requeue_blocked_without_force_after_three_flags(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Triple-flagged task")

    # Flag 3 times to reach the threshold
    for i in range(3):
        t = get_task(tmp_path, task.id)
        assert t is not None
        t.status = "flagged"
        t.pipeline_status = "implementing"
        t.flag_count = i  # simulate prior increments
        save_task(tmp_path, t)
        finish_task_run_transition(tmp_path, t, "flagged")

    # Now try to requeue without --force
    with pytest.raises(ValueError, match="flagged 3 times.*--force"):
        requeue_task(tmp_path, task.id)


def test_requeue_with_force_succeeds_after_three_flags(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Triple-flagged but forced")

    # Set flag_count to 3 and status to deferred (simulating auto-defer)
    t = get_task(tmp_path, task.id)
    assert t is not None
    t.status = "deferred"
    t.flag_count = 3
    t.pipeline_status = "implementing"
    save_task(tmp_path, t)

    # Requeue with --force should work
    result = requeue_task(tmp_path, task.id, force=True)
    assert result.status == "queued"
    assert result.flag_count == 3  # flag_count is NOT reset


def test_flag_count_not_reset_by_requeue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Counter survives requeue")

    # Flag once
    _flag_task(tmp_path, task.id)
    t = get_task(tmp_path, task.id)
    assert t is not None
    assert t.flag_count == 1

    # Requeue
    requeue_task(tmp_path, task.id)
    t = get_task(tmp_path, task.id)
    assert t is not None
    assert t.flag_count == 1  # not reset
    assert t.status == "queued"


def test_cli_requeue_warns_and_fails_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="CLI force check")

    # Set up a deferred task with flag_count >= 3
    t = get_task(tmp_path, task.id)
    assert t is not None
    t.status = "deferred"
    t.flag_count = 3
    t.pipeline_status = "implementing"
    save_task(tmp_path, t)

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=task.id, front=False, force=False)
    )
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "flagged 3 times" in output


def test_cli_requeue_succeeds_with_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="CLI force requeue")

    t = get_task(tmp_path, task.id)
    assert t is not None
    t.status = "deferred"
    t.flag_count = 3
    t.pipeline_status = "implementing"
    save_task(tmp_path, t)

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=task.id, front=True, force=True)
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "status: queued" in output

    requeued = get_task(tmp_path, task.id)
    assert requeued is not None
    assert requeued.flag_count == 3  # NOT reset


def test_new_task_has_flag_count_zero(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fresh task")
    assert task.flag_count == 0
    t = get_task(tmp_path, task.id)
    assert t is not None
    assert t.flag_count == 0
