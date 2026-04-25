"""Round-trip tests for v2 pipeline sqlite adapters.

Covers ``SqlitePersistence`` (TaskState round-trip) and ``SqliteSessionStore``
(Session round-trip keyed by task/node/engine), plus isolation invariants —
different tasks and different engines must not see each other's rows.
"""

import json
from pathlib import Path

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.common import PipelineState
from litehive.domain.recovery import (
    FailureFingerprint,
    RecoveryDisposition,
    RecoveryOutcome,
    RecoveryTrigger,
    TriggerEventKind,
)
from litehive.lifecycle.persistence import (
    FailedRunRecord,
    HookRejectFingerprint,
    LastRejection,
    LastReport,
    MergeContext,
    CommitResult,
    Limits,
    RejectionLoop,
    SqlitePersistence,
    TaskNotFound,
    TaskState,
)
from litehive.lifecycle.sessions import Session, SqliteSessionStore
from litehive.lifecycle.types import PipelineMode


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


def test_persistence_roundtrip_uses_canonical_pipeline_state(workspace: Path) -> None:
    store = SqlitePersistence(workspace)
    store.save(TaskState(task_id="T-0002", stage="after_implementing", pipeline_mode=PipelineMode.FULL))

    loaded = store.load("T-0002")

    assert loaded.stage is PipelineState.AFTER_IMPLEMENTING
    with connect_workspace_db(workspace) as connection:
        row = connection.execute("SELECT stage FROM pipeline_task_state WHERE task_id = ?", ("T-0002",)).fetchone()
    assert row["stage"] == "after_implementing"


def test_persistence_roundtrip_preserves_full_state(workspace: Path) -> None:
    store = SqlitePersistence(workspace, limits=Limits(stage_retry_limit=5))

    original = TaskState(
        task_id="T-0042",
        stage="implementing",
        pipeline_mode=PipelineMode.FULL,
        stage_retry={"implementing": 2, "testing": 1},
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="implementing",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="RuntimeError:boom",
                classification="RuntimeError",
            ),
            message="boom",
        ),
        recovery_history=[
            RecoveryOutcome(
                trigger=RecoveryTrigger(
                    origin_stage="grooming",
                    trigger_event_kind=TriggerEventKind.REJECT,
                    failure_fingerprint=FailureFingerprint(
                        fingerprint="agent:blank task",
                        classification="agent_reject",
                    ),
                    source="agent",
                    message="blank task",
                ),
                recovery_verdict="resume",
                disposition=RecoveryDisposition.RESUMED,
            )
        ],
        pre_exec_recovery_attempt=1,
        merge_context=MergeContext(
            conflict_files=["conflicted.py"],
            merge_attempt=2,
        ),
        commit_result=CommitResult(
            head_sha="abc123",
            reason="checkpoint_created",
        ),
        last_report=LastReport(
            files_changed=7,
            tests_added=3,
            changed_files=["litehive/lifecycle/prompt_serializer.py", "tests/lifecycle/test_prompt_serializer.py"],
            test_results=["uv run pytest -q tests/lifecycle/test_prompt_serializer.py -> 12 passed"],
            hook_ok=True,
        ),
        last_rejection_by_stage={
            "implementing": LastRejection(
                source="qa",
                reason="tests fail",
                raised_at_phase="testing",
            ),
        },
        failed_run_history={
            "implementing:agent:tests fail": FailedRunRecord(
                stage="implementing",
                failure_shape="agent:tests fail",
                count=2,
                first_at="2026-04-24T10:00:00+00:00",
                latest_at="2026-04-25T10:00:00+00:00",
                last_reason="tests fail",
                source="agent",
                classification="semantic_reject",
                retry_limit=5,
                failed_reason="semantic_reject",
                operator_override_count=1,
                last_operator_override_at="2026-04-24T11:00:00+00:00",
            )
        },
        rejection_loop=RejectionLoop(
            rejection_stage="testing",
            retry_target_stage="implementing",
            count=2,
        ),
        consecutive_same_hook_rejects=2,
        last_hook_reject_fingerprint=HookRejectFingerprint(
            point="after_implementing",
            command="pytest -q",
            description="timeout watchdog",
            fingerprint="after_implementing|pytest -q|timeout watchdog",
        ),
        hook_reject_recovery_invoked=True,
        failed_reason=None,
        failed_message=None,
        recovery_failure_explanation=None,
    )
    store.save(original)

    with connect_workspace_db(workspace) as connection:
        row = connection.execute("SELECT payload FROM pipeline_task_state WHERE task_id = ?", ("T-0042",)).fetchone()
    payload = json.loads(row["payload"])
    serialized_payload = json.dumps(payload, sort_keys=True)
    assert payload["active_recovery_trigger"]["failure_fingerprint"]["fingerprint"] == "RuntimeError:boom"
    assert payload["recovery_history"][0]["trigger"]["failure_fingerprint"]["fingerprint"] == "agent:blank task"
    assert "implementing:agent:tests fail" in payload["failed_run_history"]
    assert "RecoveryContext" not in serialized_payload
    assert "RecoveryRecord" not in serialized_payload
    assert "FailureDiagnostics" not in serialized_payload
    assert "failure_context" not in serialized_payload

    loaded = store.load("T-0042")
    assert loaded.task_id == original.task_id
    assert loaded.stage == "implementing"
    assert loaded.pipeline_mode == PipelineMode.FULL
    assert loaded.stage_retry == {"implementing": 2, "testing": 1}
    assert loaded.active_recovery_trigger is not None
    assert loaded.active_recovery_trigger.trigger_event_kind == TriggerEventKind.CRASH
    assert loaded.recovery_history[0].disposition == RecoveryDisposition.RESUMED
    assert loaded.pre_exec_recovery_attempt == 1
    assert loaded.merge_context is not None
    assert loaded.merge_context.conflict_files == ("conflicted.py",)
    assert loaded.merge_context.merge_attempt == 2
    assert loaded.commit_result is not None
    assert loaded.commit_result.head_sha == "abc123"
    assert loaded.commit_result.reason == "checkpoint_created"
    assert loaded.last_report.files_changed == 7
    assert loaded.last_report.tests_added == 3
    assert loaded.last_report.changed_files == [
        "litehive/lifecycle/prompt_serializer.py",
        "tests/lifecycle/test_prompt_serializer.py",
    ]
    assert loaded.last_report.test_results == [
        "uv run pytest -q tests/lifecycle/test_prompt_serializer.py -> 12 passed"
    ]
    assert loaded.last_report.hook_ok is True
    assert loaded.last_rejection_by_stage["implementing"].source == "qa"
    assert loaded.last_rejection_by_stage["implementing"].reason == "tests fail"
    failed_run = loaded.failed_run_history["implementing:agent:tests fail"]
    assert failed_run.stage == "implementing"
    assert failed_run.failure_shape == "agent:tests fail"
    assert failed_run.count == 2
    assert failed_run.latest_at == "2026-04-25T10:00:00+00:00"
    assert failed_run.operator_override_count == 1
    assert loaded.rejection_loop is not None
    assert loaded.rejection_loop.rejection_stage == "testing"
    assert loaded.rejection_loop.retry_target_stage == "implementing"
    assert loaded.rejection_loop.count == 2
    assert loaded.consecutive_same_hook_rejects == 2
    assert loaded.last_hook_reject_fingerprint is not None
    assert loaded.last_hook_reject_fingerprint.command == "pytest -q"
    assert loaded.hook_reject_recovery_invoked is True
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


def test_sessions_clear_node_sessions_removes_only_target_task_and_node(workspace: Path) -> None:
    sessions = SqliteSessionStore(workspace)
    sessions.persist("T-0001", "implementing", "codex", Session(engine_session_id="cdx-1"))
    sessions.persist("T-0001", "implementing", "claude", Session(engine_session_id="cla-1"))
    sessions.persist("T-0001", "testing", "codex", Session(engine_session_id="qa-1"))
    sessions.persist("T-0002", "implementing", "codex", Session(engine_session_id="other-1"))

    sessions.clear_node_sessions("T-0001", "implementing")

    assert sessions.get_or_create("T-0001", "implementing", "codex").engine_session_id is None
    assert sessions.get_or_create("T-0001", "implementing", "claude").engine_session_id is None
    assert sessions.get_or_create("T-0001", "testing", "codex").engine_session_id == "qa-1"
    assert sessions.get_or_create("T-0002", "implementing", "codex").engine_session_id == "other-1"
