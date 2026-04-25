from pathlib import Path

import yaml

from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import TaskActivityEntry
from litehive.state.records import create_task
from litehive.tasks.activity import legacy_task_activity_path, load_task_activity, task_activity_path
from litehive.tasks.reports import append_activity_entry


def _dump_activity(path: Path, entries: list[TaskActivityEntry]) -> None:
    path.write_text(
        yaml.safe_dump([entry.model_dump(mode="json") for entry in entries], sort_keys=False),
        encoding="utf-8",
    )


def test_append_activity_entry_persists_to_db_and_retires_legacy_file(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Activity")
    legacy_path = legacy_task_activity_path(tmp_path, task)
    activity_path = task_activity_path(tmp_path, task)
    _dump_activity(
        legacy_path,
        [TaskActivityEntry(role="qa", stage="testing", verdict="comment", message="legacy")],
    )

    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(role="swe", stage="implementing", verdict="pass", message="new"),
    )

    assert not legacy_path.exists()
    assert not activity_path.exists()
    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["legacy", "new"]


def test_load_task_activity_prefers_activity_mirror_when_both_files_exist(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Activity first")
    activity_path = task_activity_path(tmp_path, task)
    legacy_path = legacy_task_activity_path(tmp_path, task)

    _dump_activity(
        activity_path,
        [TaskActivityEntry(role="reviewer", stage="accepting", verdict="comment", message="activity mirror wins")],
    )
    _dump_activity(
        legacy_path,
        [TaskActivityEntry(role="reviewer", stage="accepting", verdict="comment", message="legacy fallback")],
    )

    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["activity mirror wins"]


def test_load_task_activity_falls_back_to_legacy_activity_file(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Legacy fallback")
    legacy_path = legacy_task_activity_path(tmp_path, task)

    _dump_activity(
        legacy_path,
        [TaskActivityEntry(role="qa", stage="testing", verdict="comment", message="legacy fallback")],
    )

    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["legacy fallback"]


def test_load_task_activity_falls_back_to_db_when_activity_mirror_is_invalid(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Activity mirror invalid")

    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(role="qa", stage="testing", verdict="comment", message="canonical db entry"),
    )
    task_activity_path(tmp_path, task).write_text("bad: [yaml", encoding="utf-8")

    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["canonical db entry"]
