from pathlib import Path

import pytest

from litehive.agents.session_store import SubagentArtifactPayload, subagent_artifacts
from litehive.agents.subagent_ids import SubagentIdRepository
from litehive.config.workspace import create_workspace
from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.runtime import Subagent
from litehive.domain.task import TaskIntentRecord, TaskRecord, TaskStateRecord, WorkspaceState
from litehive.state.store import RuntimeStore
from litehive.tasks.event_log import TaskEventLog
from litehive.workspace import Workspace


def test_runtime_store_workspace_state_roundtrip_splits_queue_from_pool_state(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    store = RuntimeStore(workspace)
    state = WorkspaceState(
        active_task_id="T-0002",
        queue=["T-0001", "T-0002"],
        pool_stop_reason="operator_pause",
        consecutive_task_failures=2,
        next_task_number=4,
    )

    store.save_workspace_state(state)
    loaded = store.load_workspace_state()
    read_only = store.load_workspace_state_read_only()

    assert loaded == state
    assert read_only == state
    with workspace.connect() as connection:
        pool_row = connection.execute("SELECT payload FROM pool_state WHERE workspace_key = 'workspace'").fetchone()
        queue_row = connection.execute("SELECT payload FROM queue WHERE workspace_key = 'workspace'").fetchone()
    assert pool_row is not None
    assert '"queue"' not in str(pool_row["payload"])
    assert queue_row is not None
    assert str(queue_row["payload"]) == '["T-0001", "T-0002"]'


def test_runtime_store_task_intent_and_state_roundtrip_preserves_indexed_status(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    store = RuntimeStore(workspace)
    intent = TaskIntentRecord(
        id="T-0001",
        slug="runtime-store",
        title="Runtime store characterization",
        goal="exercise intent storage",
        acceptance_criteria=["intent roundtrips"],
        plan=["write", "read"],
    )
    state = TaskStateRecord(
        status=TaskStatus.IN_PROGRESS,
        pipeline_status=PipelineStatus.IMPLEMENTING,
        model="gpt-test",
    )

    store.save_task_intent(intent.id, intent)
    store.save_task_state(intent.id, state)

    loaded_state = store.load_task_state(intent.id)
    assert store.load_task_intent(intent.id) == intent
    assert loaded_state is not None
    assert loaded_state.status == TaskStatus.IN_PROGRESS
    with workspace.connect() as connection:
        row = connection.execute(
            """
            SELECT lifecycle_status, pipeline_status
            FROM task_intent
            WHERE task_id = ?
            """,
            (intent.id,),
        ).fetchone()
    assert row is not None
    assert row["lifecycle_status"] == "in_progress"
    assert row["pipeline_status"] == "implementing"


def test_runtime_store_process_state_roundtrip_merges_payload_and_columns(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    store = RuntimeStore(workspace)
    payload: dict[str, object] = {
        "pid": 12345,
        "workspace": str(tmp_path),
        "command": "litehive daemon",
        "active_task_id": "T-0001",
        "log_dir": str(tmp_path / "logs"),
        "started_at": "2026-05-08T10:00:00Z",
        "heartbeat_at": "2026-05-08T10:01:00Z",
        "extra": "kept",
    }

    store.save_process_state("daemon", "running", payload)

    loaded = store.load_process_state("daemon")
    assert loaded is not None
    assert loaded["process_key"] == "daemon"
    assert loaded["status"] == "running"
    assert loaded["pid"] == 12345
    assert loaded["extra"] == "kept"

    store.clear_process_state("daemon")

    assert store.load_process_state("daemon") is None


def test_runtime_store_highest_task_number_scans_intent_and_state_tables(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    store = RuntimeStore(workspace)
    store.save_task_intent(
        "T-0007",
        TaskIntentRecord(id="T-0007", slug="intent-only", title="Intent only"),
    )
    store.save_task_state(
        "T-0012",
        TaskStateRecord(status=TaskStatus.QUEUED, pipeline_status=PipelineStatus.BACKLOG),
    )

    assert store.highest_task_number() == 12


def test_subagent_id_counter_uses_sqlite_counter_with_session_seed(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = TaskIntentRecord(id="T-0001", slug="subagent-counter", title="Subagent counter")
    store = RuntimeStore(workspace)
    store.save_task_intent(task.id, task)
    task_state = TaskStateRecord()
    task_state.subagents.append(
        Subagent(
            id="SA-0003",
            role="swe",
            engine="codex",
            status="completed",
            path="subagents/SA-0003-swe",
        )
    )
    store.save_task_state(task.id, task_state)
    subagent_artifacts(workspace, task.id, "SA-0005").save(
        session=SubagentArtifactPayload({"status": "completed"}),
    )
    task_record = TaskRecord.from_intent_and_state(task, task_state)

    first_id = SubagentIdRepository(workspace).reserve_next_id(task_record)
    second_id = SubagentIdRepository(workspace).reserve_next_id(task_record)

    assert first_id == "SA-0006"
    assert second_id == "SA-0007"
    with workspace.connect() as connection:
        row = connection.execute(
            "SELECT next_number FROM subagent_id_counters WHERE task_id = ?",
            (task.id,),
        ).fetchone()
    assert row is not None
    assert row["next_number"] == 8


def test_runtime_store_bootstrap_rebuilds_from_event_log_when_sqlite_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    calls: list[Workspace] = []

    def fake_rebuild(event_log: TaskEventLog) -> None:
        calls.append(event_log.workspace)

    monkeypatch.setattr(RuntimeStore, "_should_rebuild_from_task_event_log", lambda self: True)
    monkeypatch.setattr("litehive.tasks.event_log.TaskEventLog.rebuild_sqlite", fake_rebuild)

    RuntimeStore(workspace).bootstrap()

    assert calls == [workspace]
