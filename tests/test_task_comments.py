from pathlib import Path

import yaml

from litehive.models import TaskThreadComment
from litehive.recovery.workspace_repair import repair_workspace_state
from litehive.tasks.crud import create_task
from litehive.tasks.paths import legacy_task_thread_file, task_comments_file
from litehive.tasks.reports import append_thread_comment, load_task_thread

from tests.workspace_helpers import _cmd_repair, argparse, ensure_workspace


def _repair_args(workspace: Path) -> argparse.Namespace:
    return argparse.Namespace(workspace=workspace)


def test_append_thread_comment_writes_comments_yaml_and_preserves_legacy_entries(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Comment migration")
    legacy_path = legacy_task_thread_file(tmp_path, task)
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

    append_thread_comment(
        tmp_path,
        task,
        TaskThreadComment(role="swe", step="implementing", verdict="pass", message="new"),
    )

    assert comments_path.exists()
    assert [entry.message for entry in load_task_thread(tmp_path, task)] == ["legacy", "new"]


def test_load_task_thread_falls_back_to_legacy_thread_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Legacy comments")
    legacy_path = legacy_task_thread_file(tmp_path, task)
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

    thread = load_task_thread(tmp_path, task)

    assert len(thread) == 1
    assert thread[0].message == "legacy fallback"


def test_repair_workspace_state_migrates_legacy_thread_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Repair comments")
    legacy_path = legacy_task_thread_file(tmp_path, task)
    comments_path = task_comments_file(tmp_path, task)
    legacy_path.write_text(
        yaml.safe_dump(
            [
                {
                    "role": "recovery",
                    "step": "implementing",
                    "verdict": "comment",
                    "message": "migrate me",
                    "files_changed": [],
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    summary = repair_workspace_state(tmp_path)

    assert summary.migrated_comment_task_ids == [task.id]
    assert comments_path.exists()
    assert not legacy_path.exists()
    assert [entry.message for entry in load_task_thread(tmp_path, task)] == ["migrate me"]


def test_cmd_repair_reports_migrated_comment_tasks(
    tmp_path: Path, capsys
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Repair output")
    legacy_task_thread_file(tmp_path, task).write_text("[]\n", encoding="utf-8")

    exit_code = _cmd_repair(_repair_args(tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "migrated_comment_tasks: " + task.id in output
