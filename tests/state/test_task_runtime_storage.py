import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.task import TaskRecord
from litehive.state.records import (
    TaskStateMissingError,
    create_task,
    get_task,
    list_tasks,
    load_task_record_file,
    save_task,
    save_task_runtime,
)
from litehive.state.store import runtime_store


def _task_intent_payload(root: Path, task_id: str) -> dict:
    with connect_workspace_db(root) as connection:
        row = connection.execute("SELECT payload FROM task_intent WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return json.loads(row["payload"])


def test_get_task_reads_runtime_from_database(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="DB runtime")
    task.runtime.execution_status = "running"
    task.runtime.current_stage.stage = "implementing"
    save_task_runtime(tmp_path, task)

    loaded = get_task(tmp_path, task.id)

    assert loaded is not None
    assert loaded.runtime.execution_status == "running"
    assert loaded.runtime.current_stage.stage == "implementing"


def test_get_task_preserves_commit_sha_when_runtime_copy_is_missing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Commit SHA fallback")
    state = task.to_state_record()
    state.git.commit_sha = "abc123"
    state.runtime.git.commit_sha = None
    runtime_store(tmp_path).save_task_state(task.id, state)

    loaded = get_task(tmp_path, task.id)

    assert loaded is not None
    assert loaded.git.commit_sha == "abc123"
    assert loaded.runtime.git.commit_sha == "abc123"


def test_task_intent_persists_only_intent_fields_and_runtime_moves_to_db(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Intent only", auto_commit=False)
    task.model = "gpt-5.4"
    task.status = "flagged"
    task.flag_reason = "needs-review"
    task.flag_count = 2
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "running"
    task.runtime.current_stage.stage = "implementing"
    task.git.commit_sha = "abc123"
    task.git.checkpoint_attempts = 3
    save_task(tmp_path, task)

    task_path = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "task.yaml"
    data = _task_intent_payload(tmp_path, task.id)

    assert not task_path.exists()
    assert set(data) == {
        "id",
        "slug",
        "title",
        "created_at",
        "task_type",
        "pipeline_mode",
        "priority",
        "depends_on",
        "goal",
        "acceptance_criteria",
        "constraints",
        "plan",
        "git",
        "created_from",
    }
    assert set(data["git"]) == {"auto_commit", "commit_message"}

    loaded = get_task(tmp_path, task.id)
    assert loaded is not None
    assert loaded.model == "gpt-5.4"
    assert loaded.status == "flagged"
    assert loaded.flag_reason == "needs-review"
    assert loaded.flag_count == 2
    assert loaded.pipeline_status == "implementing"
    assert loaded.git.commit_sha == "abc123"
    assert loaded.git.checkpoint_attempts == 3
    assert loaded.runtime.execution_status == "running"
    assert loaded.runtime.current_stage.stage == "implementing"


def test_get_task_raises_when_sqlite_runtime_state_row_is_missing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    runtime_store(tmp_path).save_task_intent(
        "T-0001",
        TaskRecord(
            id="T-0001",
            slug="missing-runtime",
            title="Missing runtime row",
            pipeline_mode="full",
            priority="medium",
            git={
                "auto_commit": True,
                "commit_message": "missing runtime row",
            },
        ).to_intent_record(),
    )

    with pytest.raises(TaskStateMissingError, match="missing its SQLite runtime state row"):
        get_task(tmp_path, "T-0001")


def test_list_tasks_without_runtime_tolerates_missing_runtime_rows(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    present = create_task(tmp_path, title="Has runtime")

    runtime_store(tmp_path).save_task_intent(
        "T-0002",
        TaskRecord(
            id="T-0002",
            slug="missing-runtime",
            title="Missing runtime row",
            pipeline_mode="full",
            priority="medium",
            git={
                "auto_commit": True,
                "commit_message": "missing runtime row",
            },
        ).to_intent_record(),
    )

    tasks = list_tasks(tmp_path, include_runtime=False)

    assert [task.id for task in tasks] == [present.id, "T-0002"]


def test_load_task_record_file_rejects_legacy_intent_fields(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task_dir = tmp_path / ".litehive" / "tasks" / "T-0001-legacy-intent"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "task.yaml"
    task_path.write_text(
        yaml.safe_dump(
            {
                "id": "T-0001",
                "slug": "legacy-intent",
                "title": "Legacy intent",
                "mode": "implementation",
                "pipeline_mode": "full",
                "priority": "medium",
                "pm_complexity": "moderate",
                "planned_effort": "s",
                "human_checkpoints": [],
                "upstream_origin": None,
                "github_origin": None,
                "git": {
                    "auto_commit": True,
                    "commit_message": "legacy intent",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_task_record_file(task_path)


def test_task_record_intent_state_roundtrip_uses_model_helpers(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Roundtrip")
    task.model = "gpt-5.4"
    task.status = "flagged"
    task.flag_reason = "needs-review"
    task.flag_count = 2
    task.pipeline_status = "implementing"
    task.git.commit_sha = "abc123"
    task.git.checkpoint_attempts = 3
    task.runtime.execution_status = "running"
    task.runtime.current_stage.stage = "implementing"

    intent = task.to_intent_record()
    state = task.to_state_record()
    restored = TaskRecord.from_intent_and_state(intent, state)

    assert restored.model == "gpt-5.4"
    assert restored.status == "flagged"
    assert restored.flag_reason == "needs-review"
    assert restored.flag_count == 2
    assert restored.pipeline_status == "implementing"
    assert restored.git.commit_sha == "abc123"
    assert restored.git.checkpoint_attempts == 3
    assert restored.runtime.execution_status == "running"
    assert restored.runtime.current_stage.stage == "implementing"
