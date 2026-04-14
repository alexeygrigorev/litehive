from pathlib import Path
from typing import Any

import pytest

from litehive.lifecycle.journal import SqliteJournal
from litehive.lifecycle.registry import build_registry
from litehive.lifecycle.runner import StateMachineRunner
from litehive.roles.base import PromptContext
from litehive.lifecycle.nodes.hook import HookResult, HookRunner, HookSpec
from litehive.lifecycle.nodes.system import StubCommitNode
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.orchestration import _sync_back
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.lifecycle.sessions import InMemorySessionStore
from litehive.lifecycle.types import PipelineMode
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task
from litehive.state.persist import load_state, save_state
from litehive.tasks.queue import peek_next_task_selection


class _CircuitBreakerEngine:
    name = "stub"

    def __init__(self, recovery_outcome: str) -> None:
        self.recovery_outcome = recovery_outcome

    def run_turn(self, session: Any, prompt: Any, state: Any) -> AgentVerdict:
        session.turn_count += 1
        session.engine_session_id = f"stub-{state.task_id}-{state.stage}"
        if state.stage == "recovering":
            if self.recovery_outcome == "resume":
                return AgentVerdict(outcome="resume", metadata={"target_stage": "implementing"})
            return AgentVerdict(outcome=self.recovery_outcome)
        return AgentVerdict(outcome="pass")


class _FixedSelector:
    def __init__(self, engine: _CircuitBreakerEngine) -> None:
        self.engine = engine

    def select(self, state, node_name, excluded):
        if self.engine.name in excluded:
            return None
        return self.engine


class _FailNTimesHookRunner(HookRunner):
    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count
        self.calls: dict[tuple[str, str], int] = {}

    def run(self, spec: HookSpec, state) -> HookResult:
        key = (state.stage, spec.command)
        call = self.calls.get(key, 0) + 1
        self.calls[key] = call
        if state.stage == "after_implementing" and call <= self.fail_count:
            return HookResult(spec=spec, ok=False, output="pytest timeout")
        return HookResult(spec=spec, ok=True, output="")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ensure_workspace(tmp_path)
    return tmp_path


def _build_runner(workspace: Path, *, hook_runner: HookRunner, recovery_outcome: str) -> StateMachineRunner:
    persistence = SqlitePersistence(workspace)
    selector = _FixedSelector(_CircuitBreakerEngine(recovery_outcome))
    registry = build_registry(
        selector=selector,
        session_store=InMemorySessionStore(),
        hook_runner=hook_runner,
        commit_node=StubCommitNode(),
        prompt_context=PromptContext(workspace_root=workspace),
        hook_specs={
            "after_implementing": [
                HookSpec(
                    command="pytest -q",
                    reject_on_failure=True,
                    description="pytest timeout watchdog",
                )
            ]
        },
    )
    return StateMachineRunner(registry, persistence, journal=SqliteJournal(workspace))


def test_same_hook_reject_loop_triggers_one_recovery_and_then_resumes(workspace: Path) -> None:
    task = create_task(workspace, title="Trip breaker then recover", pipeline_mode="single")
    runner = _build_runner(
        workspace,
        hook_runner=_FailNTimesHookRunner(fail_count=3),
        recovery_outcome="resume",
    )
    persistence = SqlitePersistence(workspace)
    persistence.initialize(task.id, pipeline_mode=PipelineMode.SINGLE)

    final_state = runner.run_task(task.id)
    transitions = SqliteJournal(workspace).load_transitions(task.id)

    assert final_state.stage == "done"
    assert final_state.consecutive_same_hook_rejects == 0
    assert final_state.last_hook_reject_fingerprint is None
    assert final_state.hook_reject_recovery_invoked is False
    assert sum(1 for row in transitions if row["to_stage"] == "recovering") == 1
    assert final_state.recovery_attempt == {"after_implementing": 1}


def test_same_hook_reject_loop_flags_task_and_queue_skips_it_when_recovery_fails(workspace: Path) -> None:
    task = create_task(workspace, title="Trip breaker and stay flagged", pipeline_mode="single")
    other = create_task(workspace, title="Healthy queued task", pipeline_mode="single")
    runner = _build_runner(
        workspace,
        hook_runner=_FailNTimesHookRunner(fail_count=3),
        recovery_outcome="reject",
    )
    persistence = SqlitePersistence(workspace)
    persistence.initialize(task.id, pipeline_mode=PipelineMode.SINGLE)

    final_state = runner.run_task(task.id)
    updated = _sync_back(final_state, workspace)
    assert updated is not None
    assert updated.status == "flagged"
    assert updated.flag_reason == "hook_reject_loop"
    assert updated.runtime.consecutive_same_hook_rejects == 3
    assert updated.runtime.last_hook_reject_fingerprint is not None
    assert updated.runtime.last_hook_reject_fingerprint.command == "pytest -q"
    assert updated.runtime.hook_reject_recovery_invoked is True

    state = load_state(workspace)
    state.active_task_id = None
    state.queue = [task.id, other.id]
    save_state(workspace, state)

    selection = peek_next_task_selection(workspace)
    assert selection.task is not None
    assert selection.task.id == other.id


def test_successful_stage_progress_resets_same_hook_counter(workspace: Path) -> None:
    task = create_task(workspace, title="Hook eventually passes", pipeline_mode="single")
    runner = _build_runner(
        workspace,
        hook_runner=_FailNTimesHookRunner(fail_count=2),
        recovery_outcome="resume",
    )
    persistence = SqlitePersistence(workspace)
    persistence.initialize(task.id, pipeline_mode=PipelineMode.SINGLE)

    final_state = runner.run_task(task.id)

    assert final_state.stage == "done"
    assert final_state.recovery_attempt == {}
    assert final_state.consecutive_same_hook_rejects == 0
    assert final_state.last_hook_reject_fingerprint is None
