"""Round-trip tests for v2 pipeline sqlite adapters.

Covers ``SqlitePersistence`` (TaskState round-trip) and ``SqliteSessionStore``
(Session round-trip keyed by task/node/engine), plus isolation invariants —
different tasks and different engines must not see each other's rows.
"""

from pathlib import Path

import pytest

from litehive.pipeline.persistence import (
    LastRejection,
    LastReport,
    Limits,
    SqlitePersistence,
    TaskNotFound,
    TaskState,
)
from litehive.pipeline.sessions import Session, SqliteSessionStore
from litehive.pipeline.types import PipelineMode

from tests.workspace_helpers import ensure_workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ensure_workspace(tmp_path)
    return tmp_path


# ── SqlitePersistence ─────────────────────────────────────────────────────


def test_persistence_load_missing_raises_task_not_found(workspace: Path) -> None:
    store = SqlitePersistence(workspace)
    with pytest.raises(TaskNotFound):
        store.load("T-9999")


def test_persistence_initialize_is_idempotent(workspace: Path) -> None:
    store = SqlitePersistence(workspace)
    first = store.initialize("T-0001", pipeline_mode=PipelineMode.SINGLE)
    second = store.initialize("T-0001", pipeline_mode=PipelineMode.FULL)
    assert first.task_id == second.task_id == "T-0001"
    # Second call is a no-op: must not overwrite the existing row
    assert second.pipeline_mode == PipelineMode.SINGLE
    assert second.stage == "ready"


def test_persistence_roundtrip_preserves_full_state(workspace: Path) -> None:
    store = SqlitePersistence(workspace, limits=Limits(stage_retry_limit=5))

    original = TaskState(
        task_id="T-0042",
        stage="implementing",
        pipeline_mode=PipelineMode.FULL,
        stage_retry={"implementing": 2, "testing": 1},
        recovery_attempt={"grooming": 1},
        pre_exec_recovery_attempt=1,
        origin_stage="grooming",
        failure_context={"trigger_event": "Reject", "source": "hook", "reason": "boom"},
        last_report=LastReport(files_changed=7, tests_added=3),
        last_rejection_by_stage={
            "implementing": LastRejection(
                source="qa",
                reason="tests fail",
                raised_at_phase="testing",
            ),
        },
        failed_reason=None,
        failed_message=None,
    )
    store.save(original)

    loaded = store.load("T-0042")
    assert loaded.task_id == original.task_id
    assert loaded.stage == "implementing"
    assert loaded.pipeline_mode == PipelineMode.FULL
    assert loaded.stage_retry == {"implementing": 2, "testing": 1}
    assert loaded.recovery_attempt == {"grooming": 1}
    assert loaded.pre_exec_recovery_attempt == 1
    assert loaded.origin_stage == "grooming"
    assert loaded.failure_context["reason"] == "boom"
    assert loaded.last_report.files_changed == 7
    assert loaded.last_report.tests_added == 3
    assert loaded.last_rejection_by_stage["implementing"].source == "qa"
    assert loaded.last_rejection_by_stage["implementing"].reason == "tests fail"
    # limits is runtime config, re-injected on load
    assert loaded.limits.stage_retry_limit == 5


def test_persistence_upsert_overwrites_on_second_save(workspace: Path) -> None:
    store = SqlitePersistence(workspace)
    state = TaskState(task_id="T-0100", stage="grooming", pipeline_mode=PipelineMode.FULL)
    store.save(state)

    state.stage = "implementing"
    state.stage_retry["implementing"] = 1
    store.save(state)

    loaded = store.load("T-0100")
    assert loaded.stage == "implementing"
    assert loaded.stage_retry == {"implementing": 1}


def test_persistence_failed_reason_and_message_roundtrip(workspace: Path) -> None:
    store = SqlitePersistence(workspace)
    state = TaskState(
        task_id="T-0200",
        stage="failed",
        pipeline_mode=PipelineMode.FULL,
        failed_reason="recovery_exhausted",
        failed_message="recovery agent gave up after one attempt",
    )
    store.save(state)

    loaded = store.load("T-0200")
    assert loaded.failed_reason == "recovery_exhausted"
    assert loaded.failed_message == "recovery agent gave up after one attempt"


# ── SqliteSessionStore ────────────────────────────────────────────────────


def test_sessions_empty_get_or_create_returns_fresh(workspace: Path) -> None:
    sessions = SqliteSessionStore(workspace)
    session = sessions.get_or_create("T-0001", "implementing", "codex")
    assert session.engine_session_id is None
    assert session.conversation_id is None
    assert session.turn_count == 0


def test_sessions_persist_roundtrip(workspace: Path) -> None:
    sessions = SqliteSessionStore(workspace)
    session = Session(
        engine_session_id="cdx-abc-123",
        conversation_id="conv-xyz",
        turn_count=4,
        metadata={"last_prompt_sha": "deadbeef"},
    )
    sessions.persist("T-0001", "implementing", "codex", session)

    loaded = sessions.get_or_create("T-0001", "implementing", "codex")
    assert loaded.engine_session_id == "cdx-abc-123"
    assert loaded.conversation_id == "conv-xyz"
    assert loaded.turn_count == 4
    assert loaded.metadata == {"last_prompt_sha": "deadbeef"}
    assert loaded.resumable() is True


def test_sessions_keyed_by_task_node_engine_triple(workspace: Path) -> None:
    sessions = SqliteSessionStore(workspace)

    sessions.persist("T-0001", "implementing", "codex", Session(engine_session_id="cdx-1"))
    sessions.persist("T-0001", "implementing", "claude", Session(engine_session_id="cla-1"))
    sessions.persist("T-0001", "testing", "codex", Session(engine_session_id="cdx-2"))
    sessions.persist("T-0002", "implementing", "codex", Session(engine_session_id="cdx-3"))

    # Same task, same node, different engines → distinct sessions
    assert sessions.get_or_create("T-0001", "implementing", "codex").engine_session_id == "cdx-1"
    assert sessions.get_or_create("T-0001", "implementing", "claude").engine_session_id == "cla-1"

    # Same task, different nodes → distinct sessions
    assert sessions.get_or_create("T-0001", "testing", "codex").engine_session_id == "cdx-2"

    # Different task, same (node, engine) → distinct sessions
    assert sessions.get_or_create("T-0002", "implementing", "codex").engine_session_id == "cdx-3"


def test_sessions_upsert_replaces_existing_row(workspace: Path) -> None:
    sessions = SqliteSessionStore(workspace)
    sessions.persist("T-0001", "implementing", "codex", Session(engine_session_id="first"))
    sessions.persist("T-0001", "implementing", "codex", Session(engine_session_id="second", turn_count=5))

    loaded = sessions.get_or_create("T-0001", "implementing", "codex")
    assert loaded.engine_session_id == "second"
    assert loaded.turn_count == 5
