from tests.workspace_helpers import (
    Path,
    TaskRecord,
    _cmd_archive,
    _cmd_cleanup,
    _cmd_queue,
    _cmd_status,
    archive_done_tasks,
    archive_root,
    archive_task,
    argparse,
    cleanup_archived_tasks,
    create_task,
    ensure_workspace,
    get_task,
    list_archived_tasks,
    list_tasks,
    load_state,
    pytest,
    save_state,
    save_task,
    task_dir,
    yaml,
)


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


def test_archived_tasks_excluded_from_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Will archive")
    archive_task(tmp_path, task.id)

    exit_code = _cmd_queue(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert task.id not in output


def test_archived_tasks_excluded_from_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Will archive")
    archive_task(tmp_path, task.id)

    exit_code = _cmd_status(
        argparse.Namespace(workspace=tmp_path, fast=False, full=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert task.id not in output


# ── cleanup_archived_tasks ───────────────────────────────────────────


def test_cleanup_deletes_old_archived_tasks(tmp_path: Path) -> None:
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

    deleted = cleanup_archived_tasks(tmp_path, "30d")

    assert len(deleted) == 1
    assert deleted[0].id == task.id
    assert not archive_dir.exists()


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


# ── CLI commands ─────────────────────────────────────────────────────


def test_cmd_archive_single_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Archive via CLI")

    exit_code = _cmd_archive(
        argparse.Namespace(workspace=tmp_path, task_id=task.id)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"archived: {task.id}" in output
    assert "archived_count: 1" in output


def test_cmd_archive_all_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _make_done_task(tmp_path, "Done A")
    _make_done_task(tmp_path, "Done B")
    create_task(tmp_path, title="Still queued")

    exit_code = _cmd_archive(
        argparse.Namespace(workspace=tmp_path, task_id=None)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "archived_count: 2" in output


def test_cmd_archive_non_done_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Not done")

    exit_code = _cmd_archive(
        argparse.Namespace(workspace=tmp_path, task_id=task.id)
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "archive failed" in output


def test_cmd_cleanup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = _make_done_task(tmp_path, "Old archived")
    archive_task(tmp_path, task.id)

    # Backdate
    archive_dir = archive_root(tmp_path) / f"{task.id}-{task.slug}"
    task_yaml = archive_dir / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    from datetime import datetime, timedelta, timezone

    data["archived_at"] = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    task_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    exit_code = _cmd_cleanup(
        argparse.Namespace(workspace=tmp_path, older_than="30d")
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"deleted: {task.id}" in output
    assert "deleted_count: 1" in output


def test_cmd_cleanup_invalid_duration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_cleanup(
        argparse.Namespace(workspace=tmp_path, older_than="xyz")
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "cleanup failed" in output
