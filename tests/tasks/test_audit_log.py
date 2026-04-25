import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.paths import workspace_path
from litehive.config.workspace import ensure_workspace
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, require_task, save_task
from litehive.state.store import runtime_store
from litehive.tasks.archive import archive_task, cleanup_archived_tasks, delete_archived_task, get_archived_task
from litehive.tasks.audit import load_task_audit_entries
from litehive.tasks.status import requeue_task


def _make_done_task(root: Path, title: str) -> tuple[str, str]:
    task = create_task(root, title=title)
    task.status = "done"
    task.pipeline_status = "done"
    save_task(root, task)
    state = load_state(root)
    state.queue = [task_id for task_id in state.queue if task_id != task.id]
    save_state(root, state)
    return task.id, task.slug


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


def test_requeue_writes_durable_audit_row_to_workspace_db(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Retry with audit")
    task.status = "flagged"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    requeue_task(tmp_path, task.id, front=True, force=True)

    with sqlite3.connect(workspace_path(tmp_path, "data.db")) as connection:
        row = connection.execute(
            """
            SELECT task_id, action, source, task_status_before, task_status_after, context_json
            FROM task_audit_log
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (task.id,),
        ).fetchone()

    assert row is not None
    assert row[0] == task.id
    assert row[1] == "requeued"
    assert row[2] == "cli"
    assert row[3] == "flagged"
    assert row[4] == "queued"
    assert '"force": true' in row[5]
    assert '"front": true' in row[5]


def test_db_audit_cli_shows_requeue_entry_for_archived_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Archive after requeue")
    task.status = "flagged"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    requeue_task(tmp_path, task.id, front=True)
    archived = require_task(tmp_path, task.id)
    archived.status = "done"
    archived.pipeline_status = "done"
    save_task(tmp_path, archived)
    state = load_state(tmp_path)
    state.queue = [task_id for task_id in state.queue if task_id != archived.id]
    save_state(tmp_path, state)
    archive_task(tmp_path, archived.id)

    result = CliRunner().invoke(
        app,
        ["db", "audit", archived.id, "--action", "requeued", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "audit_entries: 1" in result.output
    assert f"task_id: {archived.id}" in result.output
    assert "action: requeued" in result.output
    assert "source: cli" in result.output
    assert '"front": true' in result.output


def test_archive_cleanup_keeps_audit_trail_after_task_removal(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task_id, _slug = _make_done_task(tmp_path, "Remove archived task")
    archive_task(tmp_path, task_id)

    _set_archived_at(tmp_path, task_id, "2025-01-01T00:00:00+00:00")

    deleted = cleanup_archived_tasks(tmp_path, "30d")

    assert [task.id for task in deleted] == [task_id]
    entries = load_task_audit_entries(tmp_path, task_id=task_id, limit=10)
    actions = {(entry.action, entry.source) for entry in entries}
    assert ("archived", "archive") in actions
    assert ("deleted", "archive_cleanup") in actions


def test_db_audit_cli_shows_deleted_tombstone_for_hard_deleted_archived_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task_id, _slug = _make_done_task(tmp_path, "Hard delete archived task")
    archive_task(tmp_path, task_id)

    archived_at = _archived_at(tmp_path, task_id)
    delete_archived_task(tmp_path, task_id, reason="Operator requested cleanup")

    result = CliRunner().invoke(
        app,
        ["db", "audit", task_id, "--action", "deleted", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "audit_entries: 1" in result.output
    assert f"task_id: {task_id}" in result.output
    assert "action: deleted" in result.output
    assert "source: archive_delete" in result.output
    assert '"archived_at": "' + archived_at + '"' in result.output
    assert '"deletion_reason": "Operator requested cleanup"' in result.output
    assert '"title": "Hard delete archived task"' in result.output
