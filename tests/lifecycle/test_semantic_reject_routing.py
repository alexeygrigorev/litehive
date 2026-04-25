import pytest

from litehive.domain.reports import SEMANTIC_REJECT_CLASSIFICATION
from litehive.domain.recovery import TriggerEventKind
from litehive.lifecycle.events import Crash, Event, Reject, Timeout
from litehive.lifecycle.journal import InMemoryJournal
from litehive.lifecycle.nodes.base import Node, NodeRegistry
from litehive.lifecycle.persistence import InMemoryPersistence, Limits, TaskState
from litehive.lifecycle.runner import StateMachineRunner
from litehive.lifecycle.types import NodeType, PipelineMode


class _FixedEventNode(Node):
    node_type = NodeType.SYSTEM

    def __init__(self, name: str, event: Event) -> None:
        self.name = name
        self.event = event

    def run(self, state: TaskState) -> Event:
        del state
        return self.event


def _run_one_transition(event: Event, state: TaskState) -> tuple[TaskState, InMemoryJournal]:
    registry = NodeRegistry()
    registry.register(_FixedEventNode(state.stage, event))
    persistence = InMemoryPersistence()
    persistence.save(state)
    journal = InMemoryJournal()
    runner = StateMachineRunner(
        registry,
        persistence,
        journal=journal,
        stop_requested=lambda: True,
    )
    return runner.run_task(state.task_id), journal


def test_semantic_reject_fails_without_recovery_trigger() -> None:
    state = TaskState(
        task_id="T-0001",
        stage="accepting",
        pipeline_mode=PipelineMode.FULL,
        stage_retry={"accepting": 1},
        limits=Limits(stage_retry_limit=1),
    )
    event = Reject(
        source="agent",
        reason="acceptance evidence is incomplete",
        classification=SEMANTIC_REJECT_CLASSIFICATION,
        metadata={"verdict_classification": SEMANTIC_REJECT_CLASSIFICATION},
    )

    final_state, journal = _run_one_transition(event, state)

    assert final_state.stage == "failed"
    assert final_state.failed_reason == "semantic_reject"
    assert final_state.active_recovery_trigger is None
    transition_payload = next(record["payload"] for record in journal.records if record["kind"] == "transition")
    assert transition_payload["event_type"] == "Reject"
    assert transition_payload["event_payload"]["classification"] == SEMANTIC_REJECT_CLASSIFICATION


@pytest.mark.parametrize(
    ("event", "expected_kind"),
    [
        (Crash(exc_type="RuntimeError", message="adapter crashed"), TriggerEventKind.CRASH),
        (Timeout(), TriggerEventKind.TIMEOUT),
    ],
)
def test_crash_and_timeout_enter_recovery(event: Event, expected_kind: TriggerEventKind) -> None:
    state = TaskState(
        task_id="T-0001",
        stage="accepting",
        pipeline_mode=PipelineMode.FULL,
    )

    final_state, _journal = _run_one_transition(event, state)

    assert final_state.stage == "recovering"
    assert final_state.active_recovery_trigger is not None
    assert final_state.active_recovery_trigger.trigger_event_kind == expected_kind
    assert final_state.failed_reason is None
