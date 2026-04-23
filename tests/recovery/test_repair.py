from pathlib import Path

import yaml
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.workspace import ensure_workspace
from litehive.recovery.execution_recovery import recover_stale_runner_state
from litehive.state.records import create_task
from litehive.tasks.activity import legacy_task_activity_path, task_activity_path
from litehive.tasks.archive import archive_root


def test_recover_stale_runner_state_skips_task_scan_for_clean_queue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ensure_workspace(tmp_path)

    def _boom(root, *, include_runtime=True):  # type: ignore[no-untyped-def]
        raise AssertionError("clean repair should not scan task records")

    monkeypatch.setattr("litehive.state.records.list_tasks", _boom)

    assert recover_stale_runner_state(tmp_path) is False


def test_repair_clean_workspace_with_100_tasks_skips_task_scan(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    for index in range(100):
        create_task(tmp_path, title=f"Task {index}")

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("clean repair should not scan task records")

    monkeypatch.setattr("litehive.state.records.list_tasks", _boom)

    result = CliRunner().invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 0
    assert "active_task_id: None" in result.output
    assert "queue_length: 100" in result.output


def test_repair_migrates_legacy_thread_yaml_to_comments_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Repair comments migration")
    legacy_path = legacy_task_activity_path(tmp_path, task)
    comments_path = task_activity_path(tmp_path, task)
    legacy_path.write_text(
        yaml.safe_dump(
            [
                {
                    "role": "qa",
                    "stage": "testing",
                    "verdict": "comment",
                    "message": "legacy discussion",
                    "created_at": "2026-04-23T12:00:00Z",
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 0
    assert "repaired: yes" in result.output
    assert comments_path.exists()
    assert not legacy_path.exists()
    payload = yaml.safe_load(comments_path.read_text(encoding="utf-8"))
    assert payload[0]["message"] == "legacy discussion"


def test_repair_migrates_archived_legacy_thread_yaml_to_comments_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    archive_task_dir = archive_root(tmp_path) / "T-9999-archived-task"
    archive_task_dir.mkdir(parents=True, exist_ok=True)
    (archive_task_dir / "task.yaml").write_text(
        yaml.safe_dump({"id": "T-9999", "title": "archived", "status": "archived"}, sort_keys=False),
        encoding="utf-8",
    )
    legacy_path = archive_task_dir / "thread.yaml"
    comments_path = archive_task_dir / "comments.yaml"
    legacy_path.write_text(
        yaml.safe_dump(
            [
                {
                    "role": "qa",
                    "stage": "testing",
                    "verdict": "comment",
                    "message": "archived legacy",
                    "created_at": "2026-04-23T12:00:00Z",
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)

    assert result.return_value == 0
    assert "repaired: yes" in result.output
    assert comments_path.exists()
    assert not legacy_path.exists()
    payload = yaml.safe_load(comments_path.read_text(encoding="utf-8"))
    assert payload[0]["message"] == "archived legacy"
