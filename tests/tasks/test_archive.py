import argparse
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.workspace import ensure_workspace
from litehive.domain.task import TaskRecord
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, get_task, list_tasks, save_task
from litehive.state.store import runtime_store
from litehive.tasks.archive import (
    archive_done_tasks,
    delete_archived_task,
    get_archived_task,
    archive_root,
    archive_task,
    cleanup_archived_tasks,
    list_archived_tasks,
)
from litehive.tasks.audit import load_task_audit_entries
from litehive.tasks.duplicates import rebuild_duplicate_task_index, search_tasks_by_text
from litehive.tasks.paths import task_dir
from litehive.tasks.queue import set_active_task
from litehive.tasks.status import requeue_task, resume_task

from tests.support.helpers import _cmd_queue, _cmd_requeue_task, _cmd_status


def _make_done_task(root: Path, title: str = "Done task") -> TaskRecord:
    """Create a task and mark it done."""
    task = create_task(root, title=title)
    task.status = "done"
    task.pipeline_status = "done"
    save_task(root, task)
    state = load_state(root)
    state.queue = [tid for tid in state.queue if tid != task.id]
    save_state(root, state)
    return get_task(root, task.id)


def _archived_at(root: Path, task_id: str) -> str:
    archived = get_archived_task(root, task_id)
    assert archived is not None
    return archived.updated_at


def _set_archived_at(root: Path, task_id: str, archived_at: str) -> None:
    archived = get_archived_task(root, task_id)
    assert archived is not None
    archived.updated_at = archived_at
    runtime_store(root).save_runtime_transaction(
        task_intents={archived.id: archived.to_intent_record()},
        task_states={archived.id: archived.to_storage_state_record()},
    )


# ── archive_task ─────────────────────────────────────────────────────


def test_archive_single_done_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Archive me")

    result = archive_task(tmp_path, task.id)

    assert result.id == task.id
    assert result.status == "archived"
    assert result.pipeline_status == "done"
    # Task dir should no longer exist under tasks/
    assert not task_dir(tmp_path, task).exists()
    # Should exist under archive/
    archive_dir = archive_root(tmp_path) / f"{task.id}-{task.slug}"
    assert archive_dir.exists()
    assert not (archive_dir / "task.yaml").exists()
    archived = get_archived_task(tmp_path, task.id)
    assert archived is not None
    assert archived.status == "archived"
    assert archived.pipeline_status == "done"
    assert archived.updated_at


def test_archive_rejects_non_done_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Still queued")

    with pytest.raises(ValueError, match="only done tasks can be archived"):
        archive_task(tmp_path, task.id)


def test_archive_rejects_unknown_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with pytest.raises(ValueError, match="not found"):
        archive_task(tmp_path, "T-9999")


# ── archive_done_tasks (bulk) ────────────────────────────────────────


def test_archive_done_tasks_bulk(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    done1 = _make_done_task(tmp_path, "Done one")
    done2 = _make_done_task(tmp_path, "Done two")
    _queued = create_task(tmp_path, title="Still queued")

    archived = archive_done_tasks(tmp_path)

    assert len(archived) == 2
    archived_ids = {t.id for t in archived}
    assert done1.id in archived_ids
    assert done2.id in archived_ids
    # Queued task should still be in tasks/
    remaining = list_tasks(tmp_path, include_runtime=False)
    assert len(remaining) == 1
    assert remaining[0].id == _queued.id


def test_archive_done_tasks_empty(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _queued = create_task(tmp_path, title="Queued")

    archived = archive_done_tasks(tmp_path)

    assert archived == []


# ── list_archived_tasks ──────────────────────────────────────────────


def test_list_archived_tasks(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "To archive")
    archive_task(tmp_path, task.id)

    archived = list_archived_tasks(tmp_path)

    assert len(archived) == 1
    assert archived[0].id == task.id


def test_list_archived_tasks_reads_sqlite_records(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Legacy archive")
    archive_task(tmp_path, task.id)

    archived = list_archived_tasks(tmp_path)

    assert len(archived) == 1
    assert archived[0].status == "archived"
    assert archived[0].pipeline_status == "done"


def test_list_archived_tasks_empty(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    archived = list_archived_tasks(tmp_path)

    assert archived == []


# ── archived tasks excluded from list/queue/status ───────────────────


def test_archived_tasks_excluded_from_list_tasks(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Will be archived")
    _queued = create_task(tmp_path, title="Visible")
    archive_task(tmp_path, task.id)

    tasks = list_tasks(tmp_path, include_runtime=False)

    task_ids = {t.id for t in tasks}
    assert task.id not in task_ids
    assert _queued.id in task_ids


def test_archived_tasks_excluded_from_queue(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Will archive")
    archive_task(tmp_path, task.id)

    exit_code = _cmd_queue(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert task.id not in output


def test_archived_tasks_excluded_from_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Will archive")
    archive_task(tmp_path, task.id)

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, fast=False, full=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert task.id not in output


def test_archive_removes_task_from_queue_and_active_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Archive active reference")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [task.id]
    save_state(tmp_path, state)
    set_active_task(tmp_path, task.id)

    archive_task(tmp_path, task.id)

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert task.id not in refreshed_state.queue


def test_archived_tasks_cannot_be_requeued_resumed_or_switched_active(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "History only")
    archive_task(tmp_path, task.id)

    with pytest.raises(ValueError, match="archived and cannot be requeued, resumed, or switched active"):
        requeue_task(tmp_path, task.id)
    with pytest.raises(ValueError, match="archived and cannot be requeued, resumed, or switched active"):
        resume_task(tmp_path, task.id)
    with pytest.raises(ValueError, match="archived and cannot be switched active"):
        set_active_task(tmp_path, task.id)


def test_cli_requeue_archived_task_directs_operator_to_create_new_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Archived history")
    archive_task(tmp_path, task.id)

    exit_code = _cmd_requeue_task(argparse.Namespace(workspace=tmp_path, task_id=task.id, front=False, force=False))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "archived and cannot be requeued" in output
    assert "Create a new task for follow-up work instead." in output


def test_archive_delete_cli_rejects_unarchived_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Queued delete target")

    result = CliRunner().invoke(
        app,
        ["archive", "delete", task.id, "--reason", "cleanup", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )

    assert result.exit_code == 0
    assert result.return_value == 1
    assert f"delete failed: Task {task.id} has status 'queued' — only archived tasks can be deleted" in result.output


def test_delete_archived_task_removes_live_records_and_preserves_tombstone(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Delete archived task")
    task.goal = "Remove archived tasks from live records and search"
    save_task(tmp_path, task)
    archive_task(tmp_path, task.id)

    archive_dir = archive_root(tmp_path) / f"{task.id}-{task.slug}"
    archived_at = _archived_at(tmp_path, task.id)

    rebuild_duplicate_task_index(tmp_path)
    archived_matches = search_tasks_by_text(tmp_path, query="remove archived tasks from live records", limit=10)
    archived_match = next(match for match in archived_matches if match.task_id == task.id)
    assert archived_match.status == "archived"

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = [task.id]
    save_state(tmp_path, state)

    deleted = delete_archived_task(tmp_path, task.id, reason="retention policy satisfied")

    refreshed_state = load_state(tmp_path)
    assert deleted.id == task.id
    assert not archive_dir.exists()
    assert get_task(tmp_path, task.id) is None
    assert get_archived_task(tmp_path, task.id) is None
    assert runtime_store(tmp_path).load_task_state(task.id) is None
    assert refreshed_state.active_task_id is None
    assert task.id not in refreshed_state.queue
    assert all(task.id != archived_task.id for archived_task in list_archived_tasks(tmp_path))
    assert all(match.task_id != task.id for match in search_tasks_by_text(tmp_path, query=task.id, limit=10))
    assert all(
        match.task_id != task.id
        for match in search_tasks_by_text(tmp_path, query="remove archived tasks from live records", limit=10)
    )

    with pytest.raises(ValueError, match=f"Task {task.id} not found"):
        requeue_task(tmp_path, task.id)
    with pytest.raises(ValueError, match=f"Task {task.id} not found"):
        resume_task(tmp_path, task.id)

    entries = load_task_audit_entries(tmp_path, task_id=task.id, limit=10)
    deleted_entry = next(entry for entry in entries if entry.action == "deleted")
    assert deleted_entry.context["title"] == task.title
    assert deleted_entry.context["archived_at"] == archived_at
    assert deleted_entry.context["deleted_at"] == deleted_entry.created_at
    assert deleted_entry.context["deletion_reason"] == "retention policy satisfied"


# ── cleanup_archived_tasks ───────────────────────────────────────────


def test_cleanup_deletes_old_archived_tasks(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Old task")
    archive_task(tmp_path, task.id)

    # Backdate archived_at to 60 days ago
    archive_dir = archive_root(tmp_path) / f"{task.id}-{task.slug}"
    from datetime import datetime, timedelta, timezone

    _set_archived_at(tmp_path, task.id, (datetime.now(timezone.utc) - timedelta(days=60)).isoformat())

    with caplog.at_level(logging.INFO, logger="litehive.tasks.archive"):
        deleted = cleanup_archived_tasks(tmp_path, "30d")

    assert len(deleted) == 1
    assert deleted[0].id == task.id
    assert not archive_dir.exists()
    assert f"Deleting archived task directory {archive_dir}" in caplog.text


def test_cleanup_keeps_recent_archived_tasks(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Recent task")
    archive_task(tmp_path, task.id)

    deleted = cleanup_archived_tasks(tmp_path, "30d")

    assert deleted == []
    archive_dir = archive_root(tmp_path) / f"{task.id}-{task.slug}"
    assert archive_dir.exists()


def test_cleanup_empty_archive(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    deleted = cleanup_archived_tasks(tmp_path, "30d")

    assert deleted == []


def test_cleanup_invalid_duration(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with pytest.raises(ValueError, match="Invalid duration format"):
        cleanup_archived_tasks(tmp_path, "foobar")


def test_cleanup_logs_target_and_raises_on_archived_task_cleanup_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Old archived task")
    archive_task(tmp_path, task.id)

    archive_dir = archive_root(tmp_path) / f"{task.id}-{task.slug}"
    from datetime import datetime, timedelta, timezone

    _set_archived_at(tmp_path, task.id, (datetime.now(timezone.utc) - timedelta(days=60)).isoformat())

    with patch("litehive.fs_cleanup.shutil.rmtree", side_effect=OSError("permission denied")):
        with caplog.at_level(logging.INFO, logger="litehive.tasks.archive"):
            with pytest.raises(OSError, match="failed to delete archived task directory .*permission denied"):
                cleanup_archived_tasks(tmp_path, "30d")

    assert f"Deleting archived task directory {archive_dir}" in caplog.text
    assert f"Failed to delete archived task directory {archive_dir}" in caplog.text
