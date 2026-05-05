import pytest

from litehive.domain.reports import SEMANTIC_REJECT_CLASSIFICATION
from litehive.domain.recovery import TriggerEventKind
from litehive.lifecycle.events import Crash, Event, Pass, Reject, Timeout
from litehive.lifecycle.nodes.base import Node, NodeRegistry
from litehive.lifecycle.persistence import Limits, TaskState
from litehive.lifecycle.runner import StateMachineRunner
from litehive.lifecycle.types import NodeType, PipelineMode
from tests.support.lifecycle_fakes import InMemoryJournal, InMemoryPersistence


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


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _TimedPassNode(Node):
    node_type = NodeType.AGENT

    def __init__(self, name: str, clock: _FakeClock, seconds: float) -> None:
        self.name = name
        self.clock = clock
        self.seconds = seconds

    def run(self, state: TaskState) -> Event:
        del state
        self.clock.advance(self.seconds)
        return Pass()


def test_task_time_budget_exceeded_fails_before_next_pre_commit_stage() -> None:
    clock = _FakeClock()
    state = TaskState(
        task_id="T-0001",
        stage="implementing",
        pipeline_mode=PipelineMode.FULL,
    )
    registry = NodeRegistry()
    registry.register(_TimedPassNode("implementing", clock, seconds=61.0))
    persistence = InMemoryPersistence()
    persistence.save(state)
    journal = InMemoryJournal()
    runner = StateMachineRunner(
        registry,
        persistence,
        journal=journal,
        task_time_budget_seconds=60.0,
        clock=clock,
    )

    final_state = runner.run_task(state.task_id)

    assert final_state.stage == "failed"
    assert final_state.failed_reason == "time_budget_exceeded"
    assert final_state.agent_elapsed_seconds == 61.0
    transition_payloads = [record["payload"] for record in journal.records if record["kind"] == "transition"]
    budget_transition = transition_payloads[-1]
    assert budget_transition["from_stage"] == "after_implementing"
    assert budget_transition["event_type"] == "TaskTimeBudgetExceeded"
    assert budget_transition["to_stage"] == "failed"
    assert budget_transition["delta"]["failed_reason"] == "time_budget_exceeded"


def test_semantic_reject_trigger_event_kind_classification() -> None:
    """Test that semantic reject events get classified with SEMANTIC_REJECT TriggerEventKind."""
    # Regular reject event
    regular_reject = Reject(
        source="agent",
        reason="implementation doesn't meet criteria",
        classification=None,
    )
    assert regular_reject.trigger_event_kind == TriggerEventKind.REJECT

    # Semantic reject event
    semantic_reject = Reject(
        source="agent",
        reason="acceptance evidence is incomplete",
        classification=SEMANTIC_REJECT_CLASSIFICATION,
    )
    assert semantic_reject.trigger_event_kind == TriggerEventKind.SEMANTIC_REJECT

    # Other event types should remain unchanged
    crash = Crash(exc_type="RuntimeError", message="adapter crashed")
    assert crash.trigger_event_kind == TriggerEventKind.CRASH
