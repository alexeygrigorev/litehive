import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.paths import workspace_path
from litehive.config.workspace import create_workspace
from litehive.state.persist import load_state_for_workspace, save_state_for_workspace
from litehive.state.records import (
    create_task_for_workspace,
    require_task_for_workspace,
    save_task_for_workspace,
)
from litehive.tasks.status import requeue_task_for_workspace
from litehive.workspace import Workspace
from litehive.domain.common import PipelineStatus, TaskStatus


def test_requeue_writes_durable_audit_row_to_workspace_db(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Retry with audit")
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task_for_workspace(workspace, task)

    requeue_task_for_workspace(workspace, task.id, front=True, force=True)

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


def test_db_audit_cli_shows_requeue_entry_for_done_task(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Complete after requeue")
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task_for_workspace(workspace, task)

    requeue_task_for_workspace(workspace, task.id, front=True)
    completed = require_task_for_workspace(workspace, task.id)
    completed.status = TaskStatus.DONE
    completed.pipeline_status = PipelineStatus.DONE
    save_task_for_workspace(workspace, completed)
    state = load_state_for_workspace(workspace)
    state.queue = [task_id for task_id in state.queue if task_id != completed.id]
    save_state_for_workspace(workspace, state)

    result = CliRunner().invoke(
        app,
        ["db", "audit", completed.id, "--action", "requeued", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "audit_entries: 1" in result.output
    assert f"task_id: {completed.id}" in result.output
    assert "action: requeued" in result.output
    assert "source: cli" in result.output
    assert '"front": true' in result.output
