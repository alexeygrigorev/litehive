import json
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.reports import TaskActivityEntry
from litehive.state.records import create_task
from litehive.tasks.activity import load_task_activity
from litehive.tasks.paths import task_dir
from litehive.tasks.reports import append_activity_entry


def _activity_rows(root: Path, task_id: str) -> list[dict]:
    with connect_workspace_db(root) as connection:
        rows = connection.execute(
            """
            SELECT entry_index, payload
            FROM task_activity
            WHERE task_id = ?
            ORDER BY entry_index
            """,
            (task_id,),
        ).fetchall()
    return [{"entry_index": row["entry_index"], "payload": json.loads(row["payload"])} for row in rows]


def test_append_activity_entry_persists_to_sqlite(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Activity")

    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(role="swe", stage="implementing", verdict="pass", message="new"),
    )

    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["new"]
    rows = _activity_rows(tmp_path, task.id)
    assert len(rows) == 1
    assert rows[0]["entry_index"] == 0
    assert rows[0]["payload"]["role"] == "swe"
    assert rows[0]["payload"]["stage"] == "implementing"
    assert rows[0]["payload"]["verdict"] == "pass"
    assert rows[0]["payload"]["message"] == "new"
    assert rows[0]["payload"]["created_at"]


def test_load_task_activity_ignores_stale_filesystem_activity(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="SQLite only")
    source_dir = task_dir(tmp_path, task)
    (source_dir / ("comments" + ".yaml")).write_text("- message: stale mirror\n", encoding="utf-8")
    (source_dir / ("thread" + ".yaml")).write_text("- message: stale legacy\n", encoding="utf-8")

    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(role="qa", stage="testing", verdict="comment", message="canonical db entry"),
    )

    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["canonical db entry"]
