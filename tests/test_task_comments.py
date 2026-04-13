from pathlib import Path

import yaml
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.models.report_models import TaskThreadComment
from litehive.state.records import create_task
from litehive.tasks.paths import legacy_task_thread_file, task_comments_file
from litehive.tasks.reports import append_thread_comment, load_task_thread

from tests.workspace_helpers import ensure_workspace


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


def test_repair_migrates_legacy_thread_yaml_to_comments_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Repair comments migration")
    legacy_path = legacy_task_thread_file(tmp_path, task)
    comments_path = task_comments_file(tmp_path, task)
    legacy_path.write_text(
        yaml.safe_dump(
            [
                {
                    "role": "qa",
                    "step": "testing",
                    "verdict": "reject",
                    "message": "legacy migration",
                    "files_changed": [],
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 0
    assert comments_path.exists()
    assert not legacy_path.exists()
    assert [entry.message for entry in load_task_thread(tmp_path, task)] == ["legacy migration"]


def test_repair_merges_legacy_thread_yaml_into_existing_comments_yaml(
    tmp_path: Path, capsys
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Repair comments merge")
    legacy_path = legacy_task_thread_file(tmp_path, task)
    comments_path = task_comments_file(tmp_path, task)
    legacy_path.write_text(
        yaml.safe_dump(
            [
                {
                    "role": "planner",
                    "step": "grooming",
                    "verdict": "pass",
                    "message": "legacy first",
                    "files_changed": [],
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    comments_path.write_text(
        yaml.safe_dump(
            [
                {
                    "role": "swe",
                    "step": "implementing",
                    "verdict": "pass",
                    "message": "canonical second",
                    "files_changed": [],
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)
    output = result.output

    assert result.return_value == 0
    assert "migrated_comment_tasks: " in output
    assert task.id in output
    assert not legacy_path.exists()
    assert [entry.message for entry in load_task_thread(tmp_path, task)] == [
        "legacy first",
        "canonical second",
    ]
