from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import TaskActivityEntry
from litehive.state.records import create_task
from litehive.tasks.reports import append_activity_entry, load_task_activity


def test_append_activity_entry_persists_task_activity(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Comments")

    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(role="swe", stage="implementing", verdict="pass", message="new"),
    )

    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["new"]
