import json
from pathlib import Path
import warnings

import pytest
from pydantic import ValidationError

from litehive.agents.session_store import save_subagent_artifacts
from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.common import OutcomeKind, OutcomeReasonCode, PipelineStatus, TaskStatus
from litehive.domain.reports import StageReport
from litehive.domain.runtime import RuntimeInterruptionState, RuntimeSubagentState, TaskRuntime
from litehive.domain.task import TaskRecord, TaskStateRecord
from litehive.state.records import (
    TaskStateMissingError,
    create_task,
    get_task,
    list_tasks,
    save_task,
    save_task_runtime,
)
from litehive.state.store import runtime_store
from litehive.tasks.report_storage import record_stage_report
from litehive.workspace import Workspace


def _task_intent_payload(root: Path, task_id: str) -> dict:
    with connect_workspace_db(root) as connection:
        row = connection.execute("SELECT payload FROM task_intent WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return json.loads(row["payload"])


def _task_state_payload(root: Path, task_id: str) -> dict:
    with connect_workspace_db(root) as connection:
        row = connection.execute("SELECT payload FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return json.loads(row["payload"])


def _task_intent_columns(root: Path, task_id: str) -> dict:
    with connect_workspace_db(root) as connection:
        row = connection.execute(
            """
            SELECT
                slug,
                title,
                created_at,
                priority,
                goal,
                acceptance_criteria_json,
                constraints_json,
                plan_json,
                dependencies_json,
                provenance_json,
                lifecycle_status,
                pipeline_status
            FROM task_intent
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def test_task_runtime_rejects_legacy_flat_payload() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskRuntime.model_validate(
            {
                "execution_status": "running",
                "retry_count": 2,
                "active_subagent": {
                    "id": "sa-1",
                    "role": "swe",
                    "engine": "codex",
                    "status": "running",
                    "path": "subagents/sa-1",
                    "started_at": "2026-04-20T10:00:00+00:00",
                    "updated_at": "2026-04-20T10:00:01+00:00",
                },
                "interruption": {
                    "source": "runner",
                    "reason": "stale state",
                    "resume_stage": "implementing",
                },
                "last_outcome": {
                    "kind": "interrupted",
                    "stage": "implementing",
                    "reason_code": "execution_interrupted",
                    "reason": "runner stopped",
                },
            }
        )


@pytest.mark.parametrize("status", ["cancelled", "wont_do", "deferred", "duplicate", "merge_failed"])
def test_task_record_rejects_removed_terminal_statuses(status: str) -> None:
    with pytest.raises(ValidationError):
        TaskRecord(id="T-0001", slug="legacy", title="Legacy", status=status)


@pytest.mark.parametrize("status", ["cancelled", "wont_do", "deferred", "duplicate", "merge_failed"])
def test_task_state_record_rejects_removed_terminal_statuses(status: str) -> None:
    with pytest.raises(ValidationError):
        TaskStateRecord(status=status)


def test_task_state_record_rejects_removed_pipeline_status() -> None:
    with pytest.raises(ValidationError):
        TaskStateRecord(pipeline_status="merge_failed")


def test_get_task_reads_runtime_from_database(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="DB runtime")
    task.runtime.pipeline.execution_status = "running"
    task.runtime.pipeline.current_stage.stage = "implementing"
    save_task_runtime(tmp_path, task)

    loaded = get_task(tmp_path, task.id)

    assert loaded is not None
    assert loaded.runtime.pipeline.execution_status == "running"
    assert loaded.runtime.pipeline.current_stage.stage == "implementing"


def test_task_runtime_persists_pipeline_and_execution_slices(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Split runtime")
    task.runtime.pipeline.execution_status = "running"
    task.runtime.pipeline.retry_count = 2
    task.runtime.pipeline.retry_limit = 4
    task.runtime.pipeline.current_stage.stage = "implementing"
    task.runtime.pipeline.last_outcome.kind = OutcomeKind.INTERRUPTED
    task.runtime.pipeline.last_outcome.stage = "implementing"
    task.runtime.pipeline.last_outcome.reason_code = OutcomeReasonCode.EXECUTION_INTERRUPTED
    task.runtime.pipeline.last_outcome.reason = "runner stopped"
    task.runtime.execution.active_subagent = RuntimeSubagentState(
        id="sa-1",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/sa-1",
        started_at="2026-04-20T10:00:00+00:00",
        updated_at="2026-04-20T10:00:01+00:00",
    )
    task.runtime.execution.interruption = RuntimeInterruptionState(
        source="runner",
        reason="stale state",
        resume_stage="implementing",
    )
    save_task_runtime(tmp_path, task)

    runtime_payload = _task_state_payload(tmp_path, task.id)["runtime"]

    assert set(runtime_payload) == {"pipeline", "execution"}
    assert "execution_status" not in runtime_payload
    assert "current_stage" not in runtime_payload
    assert "active_subagent" not in runtime_payload
    assert runtime_payload["pipeline"]["execution_status"] == "running"
    assert runtime_payload["pipeline"]["retry_count"] == 2
    assert runtime_payload["pipeline"]["last_outcome"]["reason_code"] == "execution_interrupted"
    assert runtime_payload["execution"]["active_subagent"]["id"] == "sa-1"
    assert runtime_payload["execution"]["interruption"]["resume_stage"] == "implementing"

    loaded = get_task(tmp_path, task.id)
    assert loaded is not None
    assert loaded.runtime.pipeline.execution_status == "running"
    assert loaded.runtime.pipeline.retry_count == 2
    assert loaded.runtime.pipeline.last_outcome.reason == "runner stopped"
    assert loaded.runtime.execution.active_subagent is not None
    assert loaded.runtime.execution.active_subagent.id == "sa-1"
    assert loaded.runtime.execution.interruption is not None
    assert loaded.runtime.execution.interruption.resume_stage == "implementing"


def test_current_storage_contract_uses_sqlite_without_workspace_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="SQLite contract")
    task.runtime.pipeline.execution_status = "running"
    task.runtime.pipeline.current_stage.stage = "implementing"
    save_task_runtime(tmp_path, task)
    save_subagent_artifacts(Workspace.from_path(tmp_path),
        task.id,
        "SA-0001",
        session={"id": "SA-0001", "role": "swe", "status": "completed"},
        report={"verdict": "pass", "summary": "stored in sqlite"},
        event_stream={"events": [{"type": "message", "text": "ok"}]},
    )
    report_ref = record_stage_report(Workspace.from_path(tmp_path),
        task,
        StageReport(
            task_id=task.id,
            pipeline_state="implementing",
            verdict="pass",
            summary="stored in sqlite",
            submitted_via_cli=True,
        ),
    )

    with connect_workspace_db(tmp_path) as connection:
        rows = {
            "task_intent": connection.execute(
                "SELECT COUNT(*) FROM task_intent WHERE task_id = ?",
                (task.id,),
            ).fetchone()[0],
            "task_state": connection.execute(
                "SELECT COUNT(*) FROM task_state WHERE task_id = ?",
                (task.id,),
            ).fetchone()[0],
            "subagent_sessions": connection.execute(
                "SELECT COUNT(*) FROM subagent_sessions WHERE task_id = ? AND subagent_id = ?",
                (task.id, "SA-0001"),
            ).fetchone()[0],
            "stage_reports": connection.execute(
                "SELECT COUNT(*) FROM stage_reports WHERE task_id = ?",
                (task.id,),
            ).fetchone()[0],
        }

    assert rows == {
        "task_intent": 1,
        "task_state": 1,
        "subagent_sessions": 1,
        "stage_reports": 1,
    }
    assert report_ref.display().startswith("sqlite:stage_reports/")
    assert sorted(path.relative_to(tmp_path / ".litehive") for path in (tmp_path / ".litehive").rglob("*.yaml")) == [
        Path("config.yaml")
    ]


@pytest.mark.parametrize(
    ("kind", "reason_code"),
    [
        ("done", "done"),
        ("closed", "duplicate"),
        ("closed", "execution_cancelled"),
    ],
)
def test_task_runtime_outcome_string_mutations_persist_without_pydantic_warnings(
    tmp_path: Path,
    kind: str,
    reason_code: str,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title=f"{reason_code} outcome")
    # The string spellings here are valid OutcomeKind / OutcomeReasonCode enum
    # values; this test exercises that pydantic accepts the string form on
    # assignment under validate_assignment=True without emitting warnings.
    # setattr is used so pyrefly does not complain about the str→Enum
    # assignment (the validate_assignment coercion happens at runtime).
    setattr(task.runtime.pipeline.last_outcome, "kind", kind)
    setattr(task.runtime.pipeline.last_outcome, "reason_code", reason_code)
    task.runtime.pipeline.last_outcome.reason = "runtime outcome"

    with warnings.catch_warnings():
        warnings.filterwarnings("error", message="Pydantic serializer warnings", category=UserWarning)
        save_task_runtime(tmp_path, task)

    outcome_payload = _task_state_payload(tmp_path, task.id)["runtime"]["pipeline"]["last_outcome"]

    assert outcome_payload["kind"] == kind
    assert outcome_payload["reason_code"] == reason_code
    assert outcome_payload["reason"] == "runtime outcome"


def test_get_task_preserves_commit_sha_when_runtime_copy_is_missing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Commit SHA fallback")
    state = task.to_state_record()
    state.git.commit_sha = "abc123"
    state.runtime.pipeline.git.commit_sha = None
    runtime_store(tmp_path).save_task_state(task.id, state)

    loaded = get_task(tmp_path, task.id)

    assert loaded is not None
    assert loaded.git.commit_sha == "abc123"
    assert loaded.runtime.pipeline.git.commit_sha == "abc123"


def test_task_intent_persists_only_intent_fields_and_runtime_moves_to_db(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Intent only", auto_commit=False)
    task.model = "gpt-5.4"
    task.status = TaskStatus.FLAGGED
    task.flag_reason = "needs-review"
    task.flag_count = 2
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    task.runtime.pipeline.execution_status = "running"
    task.runtime.pipeline.current_stage.stage = "implementing"
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
    assert loaded.runtime.pipeline.execution_status == "running"
    assert loaded.runtime.pipeline.current_stage.stage == "implementing"


def test_task_intent_canonical_columns_preserve_existing_runtime_status(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = TaskRecord(
        id="T-0001",
        slug="state-first",
        title="State first",
        goal="Persist canonical task metadata in SQLite.",
        acceptance_criteria=["metadata columns populated"],
        constraints=["no task.yaml"],
        plan=["save state first", "save intent second"],
        depends_on=["T-0000"],
        created_from={"source": "manual", "rationale": "test"},
    )
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    runtime_store(tmp_path).save_task_state(task.id, task.to_storage_state_record())
    runtime_store(tmp_path).save_task_intent(task.id, task.to_intent_record())

    columns = _task_intent_columns(tmp_path, task.id)

    assert columns["slug"] == "state-first"
    assert columns["title"] == "State first"
    assert columns["priority"] == "medium"
    assert columns["goal"] == "Persist canonical task metadata in SQLite."
    assert json.loads(columns["acceptance_criteria_json"]) == ["metadata columns populated"]
    assert json.loads(columns["constraints_json"]) == ["no task.yaml"]
    assert json.loads(columns["plan_json"]) == ["save state first", "save intent second"]
    assert json.loads(columns["dependencies_json"]) == ["T-0000"]
    assert json.loads(columns["provenance_json"])["source"] == "manual"
    assert columns["lifecycle_status"] == "flagged"
    assert columns["pipeline_status"] == "implementing"


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


def test_task_record_intent_state_roundtrip_uses_model_helpers(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Roundtrip")
    task.model = "gpt-5.4"
    task.status = TaskStatus.FLAGGED
    task.flag_reason = "needs-review"
    task.flag_count = 2
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    task.git.commit_sha = "abc123"
    task.git.checkpoint_attempts = 3
    task.runtime.pipeline.execution_status = "running"
    task.runtime.pipeline.current_stage.stage = "implementing"

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
    assert restored.runtime.pipeline.execution_status == "running"
    assert restored.runtime.pipeline.current_stage.stage == "implementing"
