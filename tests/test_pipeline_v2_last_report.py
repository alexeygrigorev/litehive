"""Pass.metadata → state.last_report wiring tests.

Agent verdicts carry ``files_changed`` / ``tests_added`` in their
metadata. The Runner copies that into ``state.last_report`` after each
transition so guards like ``zero_change_shortcut`` see real numbers.
"""

from typing import Any

from litehive.lifecycle.events import Pass
from litehive.lifecycle.nodes.agent import AgentNode, AgentVerdict
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.runner import StateMachineRunner
from litehive.lifecycle.sessions import InMemorySessionStore
from litehive.lifecycle.types import PipelineMode


class _FixedSelector:
    def __init__(self, engine):
        self.engine = engine

    def select(self, state, node_name, excluded):
        return None if self.engine.name in excluded else self.engine


class _ReportingEngine:
    name = "stub"

    def run_turn(self, session: Any, prompt: Any, state: Any) -> AgentVerdict:
        return AgentVerdict(
            outcome="pass",
            reason="ok",
            metadata={"files_changed": ["a.py", "b.py", "c.py"], "tests_added": 2},
        )


class _TrivialAgent(AgentNode):
    def build_prompt(self, state: TaskState) -> dict:
        return {"stage": self.name}


def test_agent_verdict_metadata_flows_into_pass_event() -> None:
    """Verify AgentNode._verdict_to_event copies metadata onto Pass."""
    agent = _TrivialAgent(
        "implementing",
        _FixedSelector(_ReportingEngine()),
        InMemorySessionStore(),
    )
    event = agent.run(
        TaskState(task_id="T-0001", stage="implementing", pipeline_mode=PipelineMode.FULL)
    )
    assert isinstance(event, Pass)
    assert event.metadata["files_changed"] == ["a.py", "b.py", "c.py"]
    assert event.metadata["tests_added"] == 2


def test_runner_updates_state_last_report_from_pass_metadata() -> None:
    """Runner._apply_event_side_effects copies Pass.metadata into state.last_report."""
    state = TaskState(task_id="T-0001", stage="implementing", pipeline_mode=PipelineMode.FULL)
    event = Pass(metadata={"files_changed": ["x.py", "y.py"], "tests_added": 3})

    StateMachineRunner._apply_event_side_effects(state, event)

    assert state.last_report.files_changed == 2
    assert state.last_report.tests_added == 3
