"""Tests for the `litehive task recent` command."""

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.state.records import create_task, save_task

from tests.support.helpers import _cmd_recent


def _insert_transition(
    root: Path,
    *,
    task_id: str,
    seq: int,
    created_at: datetime,
    from_stage: str,
    to_stage: str,
    event_type: str = "pass",
) -> None:
    with connect_workspace_db(root) as connection:
        connection.execute(
            """
            INSERT INTO pipeline_transitions (
                task_id, seq, created_at,
                from_stage, event_type, event_payload,
                to_stage, rule_description, delta
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                seq,
                created_at.replace(microsecond=0).isoformat(),
                from_stage,
                event_type,
                "{}",
                to_stage,
                f"{from_stage} -> {to_stage}",
                "{}",
            ),
        )
        connection.commit()


def test_task_recent_default_window_lists_recent_tasks_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)

    recent = create_task(tmp_path, title="Recent task", auto_commit=False)
    recent.status = "in_progress"
    recent.pipeline_status = "implementing"
    save_task(tmp_path, recent)

    stale = create_task(tmp_path, title="Stale task", auto_commit=False)
    stale.status = "done"
    stale.pipeline_status = "done"
    save_task(tmp_path, stale)

    _insert_transition(
        tmp_path,
        task_id=recent.id,
        seq=0,
        created_at=now - timedelta(hours=2),
        from_stage="backlog",
        to_stage="grooming",
    )
    _insert_transition(
        tmp_path,
        task_id=recent.id,
        seq=1,
        created_at=now - timedelta(hours=1, minutes=15),
        from_stage="grooming",
        to_stage="implementing",
    )
    _insert_transition(
        tmp_path,
        task_id=stale.id,
        seq=0,
        created_at=now - timedelta(days=2),
        from_stage="backlog",
        to_stage="done",
    )

    exit_code = _cmd_recent(argparse.Namespace(workspace=tmp_path, since=None))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert recent.id in output
    assert "Recent task" in output
    assert stale.id not in output
    assert "Stale task" not in output


def test_task_recent_since_renders_expected_fields(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)

    shown = create_task(tmp_path, title="Touched recently", auto_commit=False)
    shown.status = "in_progress"
    shown.pipeline_status = "implementing"
    save_task(tmp_path, shown)

    hidden = create_task(tmp_path, title="Older touch", auto_commit=False)
    hidden.status = "queued"
    hidden.pipeline_status = "backlog"
    save_task(tmp_path, hidden)

    _insert_transition(
        tmp_path,
        task_id=shown.id,
        seq=0,
        created_at=now - timedelta(minutes=40),
        from_stage="backlog",
        to_stage="grooming",
    )
    _insert_transition(
        tmp_path,
        task_id=shown.id,
        seq=1,
        created_at=now - timedelta(minutes=10),
        from_stage="grooming",
        to_stage="implementing",
    )
    _insert_transition(
        tmp_path,
        task_id=hidden.id,
        seq=0,
        created_at=now - timedelta(hours=2),
        from_stage="backlog",
        to_stage="grooming",
    )

    exit_code = _cmd_recent(argparse.Namespace(workspace=tmp_path, since="1h"))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "TASK_ID" in output
    assert "TITLE" in output
    assert "TRANSITIONS" in output
    assert "ELAPSED" in output
    assert "FINAL_STAGE" in output
    assert "STATUS" in output
    assert shown.id in output
    assert "Touched recently" in output
    assert "2" in output
    assert "30m00s" in output
    assert "implementing" in output
    assert "in_progress" in output
    assert hidden.id not in output
    assert "Older touch" not in output


def test_task_recent_rejects_invalid_since(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_recent(argparse.Namespace(workspace=tmp_path, since="yesterday"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "recent failed: Invalid duration format 'yesterday'" in output
