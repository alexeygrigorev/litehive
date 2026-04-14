from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import TaskThreadComment
from litehive.state.records import create_task
from litehive.tasks.paths import task_comments_file
from litehive.tasks.reports import append_thread_comment, load_task_thread


def test_append_thread_comment_writes_comments_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Comments")

    append_thread_comment(
        tmp_path,
        task,
        TaskThreadComment(role="swe", step="implementing", verdict="pass", message="new"),
    )

    comments_path = task_comments_file(tmp_path, task)
    assert comments_path.exists()
    assert [entry.message for entry in load_task_thread(tmp_path, task)] == ["new"]
