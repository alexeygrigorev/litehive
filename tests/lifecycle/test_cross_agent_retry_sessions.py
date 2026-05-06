from pathlib import Path
from typing import Any

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.domain.common import PipelineState
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.nodes.hook import HookRunner, HookSpec
from litehive.lifecycle.nodes.system import StubCommitNode
from litehive.lifecycle.journal import SqliteJournal
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.lifecycle.registry import build_registry
from litehive.lifecycle.runner import StateMachineRunner
from litehive.lifecycle.sessions import SqliteSessionStore
from litehive.lifecycle.types import PipelineMode
from litehive.roles.base import PromptContext
from litehive.state.records import create_task
from litehive.workspace import Workspace


class _FixedSelector:
    def __init__(self, engine) -> None:
        self.engine = engine

    def select(self, state, node_name, excluded):
        if self.engine.name in excluded:
            return None
        return self.engine


class _NoopHookRunner(HookRunner):
    def run(self, spec: HookSpec, state):
        del spec, state
        return None


class _CrossAgentRetryEngine:
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._per_stage_calls: dict[str, int] = {}

    def run_turn(self, session: Any, prompt: Any, state: Any) -> AgentVerdict:
        self.calls.append((state.stage, session.engine_session_id))
        call = self._per_stage_calls.get(state.stage, 0) + 1
        self._per_stage_calls[state.stage] = call

        if state.stage == "testing" and call == 1:
            session.engine_session_id = "testing-1"
            return AgentVerdict(outcome="reject", reason="fix the implementation")

        session.engine_session_id = f"{state.stage}-{call}"
        return AgentVerdict(outcome="pass")


class _SameAgentRetryEngine:
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._implementing_calls = 0

    def run_turn(self, session: Any, prompt: Any, state: Any) -> AgentVerdict:
        self.calls.append((state.stage, session.engine_session_id))
        if state.stage == "implementing":
            self._implementing_calls += 1
            session.engine_session_id = f"implementing-{self._implementing_calls}"
            if self._implementing_calls == 1:
                return AgentVerdict(outcome="reject", reason="keep iterating")
        else:
            session.engine_session_id = f"{state.stage}-1"
        return AgentVerdict(outcome="pass")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ensure_workspace(tmp_path)
    return tmp_path


def _build_runner(
    workspace: Path,
    *,
    engine,
) -> tuple[StateMachineRunner, SqliteSessionStore]:
    persistence = SqlitePersistence(Workspace.from_path(workspace))
    sessions = SqliteSessionStore(Workspace.from_path(workspace))
    registry = build_registry(
        selector=_FixedSelector(engine),
        session_store=sessions,
        hook_runner=_NoopHookRunner(),
        commit_node=StubCommitNode(),
        prompt_context=PromptContext(workspace=Workspace.from_path(workspace)),
    )
    return (
        StateMachineRunner(registry, persistence, journal=SqliteJournal(Workspace.from_path(workspace)), session_store=sessions),
        sessions,
    )


def test_cross_agent_reject_clears_target_stage_sessions(workspace: Path) -> None:
    task = create_task(workspace, title="QA sends SWE back", pipeline_mode="full")
    persistence = SqlitePersistence(Workspace.from_path(workspace))
    persistence.initialize(task.id, pipeline_mode=PipelineMode.FULL)
    engine = _CrossAgentRetryEngine()
    runner, sessions = _build_runner(
        workspace,
        engine=engine,
    )

    final_state = runner.run_task(task.id)

    assert final_state.stage == "done"
    implementing_calls = [call for call in engine.calls if call[0] == "implementing"]
    assert implementing_calls == [
        ("implementing", None),
        ("implementing", None),
    ]
    persisted = sessions.get_or_create(task.id, PipelineState.IMPLEMENTING, engine.name)
    assert persisted.engine_session_id == "implementing-2"
    transitions = SqliteJournal(Workspace.from_path(workspace)).load_transitions(task.id)
    implementing_attempts = [
        transition
        for transition in transitions
        if transition["from_stage"] == "implementing" and transition["event_type"] == "Pass"
    ]
    assert len(implementing_attempts) == 2


def test_same_agent_reject_keeps_stage_session_continuity(workspace: Path) -> None:
    task = create_task(workspace, title="SWE retries in place", pipeline_mode="single")
    persistence = SqlitePersistence(Workspace.from_path(workspace))
    persistence.initialize(task.id, pipeline_mode=PipelineMode.SINGLE)
    engine = _SameAgentRetryEngine()
    runner, sessions = _build_runner(
        workspace,
        engine=engine,
    )

    final_state = runner.run_task(task.id)

    assert final_state.stage == "done"
    implementing_calls = [call for call in engine.calls if call[0] == "implementing"]
    assert implementing_calls == [
        ("implementing", None),
        ("implementing", "implementing-1"),
    ]
    persisted = sessions.get_or_create(task.id, PipelineState.IMPLEMENTING, engine.name)
    assert persisted.engine_session_id == "implementing-2"
