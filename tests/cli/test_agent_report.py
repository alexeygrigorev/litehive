from pathlib import Path

import yaml
from typer.testing import CliRunner

from litehive.cli.agent_cli import agent_app
from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import TaskThreadComment
from litehive.state.records import get_task_record
from litehive.tasks.paths import task_comments_file
from litehive.tasks.reports import load_task_thread


def test_agent_report_uses_intent_record_when_runtime_row_is_missing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task_dir = tmp_path / ".litehive" / "tasks" / "T-0001-missing-runtime"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "T-0001",
                "slug": "missing-runtime",
                "title": "Missing runtime row",
                "pipeline_mode": "full",
                "priority": "medium",
                "git": {
                    "auto_commit": True,
                    "commit_message": "missing runtime row",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "recovery completed",
            "--role",
            "recovery",
            "--step",
            "grooming",
            "--task-id",
            "T-0001",
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    task = get_task_record(tmp_path, "T-0001")
    assert task is not None
    comments_path = task_comments_file(tmp_path, task)
    assert comments_path.exists()
    comments = load_task_thread(tmp_path, task)
    assert comments == [
        TaskThreadComment(
            role="recovery",
            step="grooming",
            verdict="pass",
            message="recovery completed",
            files_changed=[],
        )
    ]


def test_agent_report_uses_env_stage_when_runtime_row_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    ensure_workspace(tmp_path)
    task_dir = tmp_path / ".litehive" / "tasks" / "T-0001-missing-runtime"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "T-0001",
                "slug": "missing-runtime",
                "title": "Missing runtime row",
                "pipeline_mode": "full",
                "priority": "medium",
                "git": {
                    "auto_commit": True,
                    "commit_message": "missing runtime row",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITEHIVE_STAGE", "grooming")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "planner completed",
            "--role",
            "planner",
            "--task-id",
            "T-0001",
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    task = get_task_record(tmp_path, "T-0001")
    assert task is not None
    comments = load_task_thread(tmp_path, task)
    assert comments == [
        TaskThreadComment(
            role="planner",
            step="grooming",
            verdict="pass",
            message="planner completed",
            files_changed=[],
        )
    ]
