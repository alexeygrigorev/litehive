from pathlib import Path

import yaml

from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import TaskActivityEntry
from litehive.state.records import create_task
from litehive.tasks.activity import legacy_task_activity_path, task_activity_path
from litehive.tasks.reports import append_activity_entry, load_task_activity


def _dump_comments(path: Path, entries: list[TaskActivityEntry]) -> None:
    path.write_text(
        yaml.safe_dump([entry.model_dump(mode="json") for entry in entries], sort_keys=False),
        encoding="utf-8",
    )


def test_append_activity_entry_writes_comments_yaml_and_retires_legacy_thread(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Activity")
    legacy_path = legacy_task_activity_path(tmp_path, task)
    comments_path = task_activity_path(tmp_path, task)
    _dump_comments(
        legacy_path,
        [TaskActivityEntry(role="qa", stage="testing", verdict="comment", message="legacy")],
    )

    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(role="swe", stage="implementing", verdict="pass", message="new"),
    )

    assert comments_path.name == "comments.yaml"
    assert not legacy_path.exists()
    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["legacy", "new"]
    on_disk = yaml.safe_load(comments_path.read_text(encoding="utf-8"))
    assert [entry["message"] for entry in on_disk] == ["legacy", "new"]


def test_load_task_activity_prefers_comments_yaml_when_both_files_exist(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Comments first")
    comments_path = task_activity_path(tmp_path, task)
    legacy_path = legacy_task_activity_path(tmp_path, task)

    _dump_comments(
        comments_path,
        [TaskActivityEntry(role="reviewer", stage="accepting", verdict="comment", message="comments wins")],
    )
    _dump_comments(
        legacy_path,
        [TaskActivityEntry(role="reviewer", stage="accepting", verdict="comment", message="legacy fallback")],
    )

    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["comments wins"]


def test_load_task_activity_falls_back_to_legacy_thread_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Legacy fallback")
    legacy_path = legacy_task_activity_path(tmp_path, task)

    _dump_comments(
        legacy_path,
        [TaskActivityEntry(role="qa", stage="testing", verdict="comment", message="legacy fallback")],
    )

    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["legacy fallback"]


def test_load_task_activity_falls_back_to_db_when_comments_yaml_is_invalid(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Comments mirror invalid")

    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(role="qa", stage="testing", verdict="comment", message="canonical db entry"),
    )
    task_activity_path(tmp_path, task).write_text("bad: [yaml", encoding="utf-8")

    assert [entry.message for entry in load_task_activity(tmp_path, task)] == ["canonical db entry"]
