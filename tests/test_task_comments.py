from pathlib import Path

import yaml
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.domain.reports import TaskThreadComment
from litehive.state.records import create_task
from litehive.tasks.paths import task_comments_file, task_dir
from litehive.tasks.reports import append_thread_comment, load_task_thread

from tests.workspace_helpers import ensure_workspace


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


def test_load_task_thread_ignores_removed_thread_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Legacy comments")
    legacy_path = task_dir(tmp_path, task) / "thread.yaml"
    legacy_path.write_text(
        yaml.safe_dump(
            [
                {
                    "role": "qa",
                    "step": "testing",
                    "verdict": "reject",
                    "message": "legacy fallback",
                    "files_changed": [],
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert load_task_thread(tmp_path, task) == []


def test_repair_does_not_migrate_removed_thread_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Repair comments")
    legacy_path = task_dir(tmp_path, task) / "thread.yaml"
    comments_path = task_comments_file(tmp_path, task)
    legacy_path.write_text(
        yaml.safe_dump(
            [
                {
                    "role": "planner",
                    "step": "grooming",
                    "verdict": "pass",
                    "message": "legacy",
                    "files_changed": [],
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 0
    assert "migrated_comment_tasks:" not in result.output
    assert legacy_path.exists()
    assert not comments_path.exists()
