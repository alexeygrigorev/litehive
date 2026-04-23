from pathlib import Path

import yaml

from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import TaskActivityEntry
from litehive.state.records import create_task
from litehive.tasks.activity import legacy_task_activity_path, task_activity_path
from litehive.tasks.reports import append_activity_entry


def test_snapshot_uses_comments_metadata(monkeypatch, tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(repo_root / "litehive-web"))

    from litehive_web.common import iter_stream_paths
    from litehive_web.snapshot import serialize_task

    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Web comments metadata")
    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(role="reviewer", stage="accepting", verdict="comment", message="ship it"),
    )

    payload = serialize_task(tmp_path, task, None)

    assert payload["comments_file"].endswith("/comments.yaml")
    assert [comment["message"] for comment in payload["comments"]] == ["ship it"]
    assert "thread_file" not in payload
    assert "thread" not in payload
    assert task_activity_path(tmp_path, task) in iter_stream_paths(tmp_path)


def test_snapshot_falls_back_to_legacy_comments_metadata(monkeypatch, tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(repo_root / "litehive-web"))

    from litehive_web.snapshot import serialize_task

    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Web legacy comments metadata")
    legacy_task_activity_path(tmp_path, task).write_text(
        yaml.safe_dump(
            [
                {
                    "role": "reviewer",
                    "stage": "accepting",
                    "verdict": "comment",
                    "message": "legacy snapshot",
                    "created_at": "2026-04-23T12:00:00Z",
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    payload = serialize_task(tmp_path, task, None)

    assert payload["comments_file"].endswith("/thread.yaml")
    assert [comment["message"] for comment in payload["comments"]] == ["legacy snapshot"]
