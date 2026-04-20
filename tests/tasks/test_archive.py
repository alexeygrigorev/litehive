import argparse
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from litehive.config.workspace import ensure_workspace
from litehive.domain.task import TaskRecord
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, get_task, list_tasks, save_task
from litehive.tasks.archive import (
    archive_done_tasks,
    archive_root,
    archive_task,
    cleanup_archived_tasks,
    list_archived_tasks,
)
from litehive.tasks.paths import task_dir

from tests.support.helpers import _cmd_queue, _cmd_status


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


# ── archive_task ─────────────────────────────────────────────────────


def test_archive_single_done_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Archive me")

    result = archive_task(tmp_path, task.id)

    assert result.id == task.id
    # Task dir should no longer exist under tasks/
    assert not task_dir(tmp_path, task).exists()
    # Should exist under archive/
    archive_dir = archive_root(tmp_path) / f"{task.id}-{task.slug}"
    assert archive_dir.exists()
    # archived_at should be set in task.yaml
    data = yaml.safe_load((archive_dir / "task.yaml").read_text(encoding="utf-8"))
    assert "archived_at" in data


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


def test_list_archived_tasks_defaults_legacy_records_to_done(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Legacy archive")
    archive_task(tmp_path, task.id)

    archive_dir = archive_root(tmp_path) / f"{task.id}-{task.slug}"
    task_yaml = archive_dir / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    data.pop("status", None)
    data.pop("pipeline_status", None)
    task_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    archived = list_archived_tasks(tmp_path)

    assert len(archived) == 1
    assert archived[0].status == "done"
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
    task_yaml = archive_dir / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    from datetime import datetime, timedelta, timezone

    data["archived_at"] = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    task_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

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
    task_yaml = archive_dir / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    from datetime import datetime, timedelta, timezone

    data["archived_at"] = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    task_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with patch("litehive.fs_cleanup.shutil.rmtree", side_effect=OSError("permission denied")):
        with caplog.at_level(logging.INFO, logger="litehive.tasks.archive"):
            with pytest.raises(OSError, match="failed to delete archived task directory .*permission denied"):
                cleanup_archived_tasks(tmp_path, "30d")

    assert f"Deleting archived task directory {archive_dir}" in caplog.text
    assert f"Failed to delete archived task directory {archive_dir}" in caplog.text
