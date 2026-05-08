"""Unit tests for AgentNode retry / backoff / nudge / engine-switch paths.

These run AgentNode.run() directly with scripted engines so each branch is
exercised without the full state machine.
"""

from pathlib import Path
from typing import Any

import pytest

from heru.base import CLIExecutionResult
from heru.types import RuntimeEngineContinuation, SubagentRef

from litehive.domain.agent import EngineFailure, ExecutionTrace, SubagentResult
from litehive.lifecycle.events import Crash, Pass, Reject
from litehive.lifecycle.heru_factory import HeruEngineAdapter
from litehive.lifecycle.nodes.agent import (
    AgentNode,
    AgentVerdict,
    NudgeRequired,
    TransientError,
    UnrecoverableError,
)
from litehive.domain.common import PipelineState
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.prompt_types import AgentPrompt
from litehive.lifecycle.types import PipelineMode
from litehive.workspace import Workspace
from litehive.state.records import create_task_for_workspace
from tests.support.lifecycle_fakes import InMemorySessionStore


def _stub_execution(exit_code: int = 0, stdout: str = "", stderr: str = "") -> CLIExecutionResult:
    return CLIExecutionResult(
        adapter="test",
        argv=("test",),
        cwd=Path("/tmp"),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        pid=0,
    )


class _ScriptedEngine:
    """Walks through a scripted sequence of outcomes on every ``run_turn``."""

    def __init__(self, name: str, script: list) -> None:
        self.name = name
        self.script = list(script)
        self.calls = 0
        self.prompts_seen: list[Any] = []
        self.session_ids: list[int] = []

    def run_turn(self, session: Any, prompt: Any, state: TaskState) -> AgentVerdict:
        self.calls += 1
        # Track per-call what prompt was given so tests can inspect nudges
        self.prompts_seen.append(prompt)
        self.session_ids.append(id(session))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step  # AgentVerdict


class _ListSelector:
    """Returns engines in order from a list, honoring ``excluded``."""

    def __init__(self, engines: list) -> None:
        self.engines = engines

    def select(self, state, node_name, excluded):
        for engine in self.engines:
            if engine.name not in excluded:
                return engine
        return None


class _TrivialAgent(AgentNode):
    """AgentNode concrete subclass that builds a trivial dict prompt."""

    def build_prompt(self, state: TaskState) -> dict:
        return {"stage": self.name, "task_id": state.task_id}


class _HeruPromptAgent(AgentNode):
    """AgentNode that emits the prompt shape expected by HeruEngineAdapter."""

    def build_prompt(self, state: TaskState) -> AgentPrompt:
        del state  # task identity is sourced from the TaskState passed to run_turn
        return AgentPrompt(
            role="swe",
            stage=PipelineState(self.name),
            pipeline_mode=PipelineMode.FULL,
            stage_retry=0,
            instruction_variant="fresh",
            instruction_layers=[],
            last_report={},
            last_rejection=None,
            failed_run_history=[],
            runner_hooks=[],
        )


def make_state(**overrides) -> TaskState:
    return TaskState(
        task_id=overrides.get("task_id", "T-0001"),
        stage=overrides.get("stage", "implementing"),
        pipeline_mode=overrides.get("pipeline_mode", PipelineMode.FULL),
    )


# ── tier-1 retry on TransientError ───────────────────────────────────────


def test_transient_error_retries_same_engine_and_succeeds() -> None:
    engine = _ScriptedEngine(
        "codex",
        [
            TransientError("network blip", failure_kind="network"),
            TransientError("network blip", failure_kind="network"),
            AgentVerdict(outcome="pass"),
        ],
    )
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([engine]),
        InMemorySessionStore(),
        retry_budget=3,
        retry_on=("network",),
    )
    event = node.run(make_state())
    assert isinstance(event, Pass)
    assert engine.calls == 3


def test_retry_exhaustion_becomes_engine_switch() -> None:
    """Retry budget exhausted on codex → claude gets its own fresh session."""
    codex = _ScriptedEngine("codex", [TransientError("x", failure_kind="timeout")] * 3)
    claude = _ScriptedEngine("claude", [AgentVerdict(outcome="pass")])
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([codex, claude]),
        InMemorySessionStore(),
        retry_budget=3,
    )
    event = node.run(make_state())
    assert isinstance(event, Pass)
    assert codex.calls == 3
    assert claude.calls == 1


def test_all_engines_exhausted_returns_crash() -> None:
    codex = _ScriptedEngine("codex", [TransientError("a", failure_kind="timeout")] * 3)
    claude = _ScriptedEngine("claude", [TransientError("b", failure_kind="timeout")] * 3)
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([codex, claude]),
        InMemorySessionStore(),
        retry_budget=3,
    )
    event = node.run(make_state())
    assert isinstance(event, Crash)
    assert event.exc_type == "AllEnginesExhausted"


# ── retry backoff ───────────────────────────────────────────────────────


def test_retry_backoff_sleeps_between_attempts() -> None:
    sleeps: list[float] = []
    engine = _ScriptedEngine(
        "codex",
        [
            TransientError("x", failure_kind="timeout"),
            TransientError("x", failure_kind="timeout"),
            AgentVerdict(outcome="pass"),
        ],
    )
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([engine]),
        InMemorySessionStore(),
        retry_budget=3,
        retry_backoff_seconds=1.0,
        retry_backoff_multiplier=2.0,
        sleep_fn=sleeps.append,
    )
    event = node.run(make_state())
    assert isinstance(event, Pass)
    # First attempt runs immediately; attempts 2 and 3 sleep first.
    # Exponential: 1.0 * 2^0 = 1.0, 1.0 * 2^1 = 2.0
    assert sleeps == [1.0, 2.0]


# ── nudge on missing verdict ────────────────────────────────────────────


def test_nudge_required_reissues_turn_with_nudge_prompt() -> None:
    engine = _ScriptedEngine(
        "codex",
        [
            NudgeRequired("no litehive agent report call"),
            AgentVerdict(outcome="pass"),
        ],
    )
    store = InMemorySessionStore()
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([engine]),
        store,
        retry_budget=3,
    )
    event = node.run(make_state())
    assert isinstance(event, Pass)
    assert engine.calls == 2
    # Verify the second call received a nudged prompt
    prompts = engine.prompts_seen
    assert len(prompts) == 2
    assert "nudge" not in prompts[0] or not prompts[0].get("nudge")
    assert prompts[1]["nudge"] is True
    assert "nudge_message" in prompts[1]


def test_nudge_keeps_existing_session_continuation() -> None:
    seen_session_ids: list[str | None] = []

    class _NudgingEngine:
        name = "codex"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, session: Any, prompt: Any, state: TaskState) -> AgentVerdict:
            self.calls += 1
            seen_session_ids.append(session.engine_session_id)
            if self.calls == 1:
                session.engine_session_id = "resume-123"
                raise NudgeRequired("no litehive agent report call")
            return AgentVerdict(outcome="pass")

    engine = _NudgingEngine()
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([engine]),
        InMemorySessionStore(),
        retry_budget=3,
    )

    event = node.run(make_state())

    assert isinstance(event, Pass)
    assert seen_session_ids == [None, "resume-123"]


def test_nudge_does_not_consume_retry_budget() -> None:
    """Nudge + retry should both work independently of each other's budgets."""
    engine = _ScriptedEngine(
        "codex",
        [
            NudgeRequired("missed"),  # nudge
            TransientError("blip", failure_kind="timeout"),  # tier-1 retry (1/3)
            TransientError("blip", failure_kind="timeout"),  # tier-1 retry (2/3)
            AgentVerdict(outcome="pass"),  # success
        ],
    )
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([engine]),
        InMemorySessionStore(),
        retry_budget=3,
    )
    event = node.run(make_state())
    assert isinstance(event, Pass)
    assert engine.calls == 4


def test_agent_node_preserves_reject_source_and_metadata() -> None:
    engine = _ScriptedEngine(
        "codex",
        [
            AgentVerdict(
                outcome="reject",
                reason="filesystem guard caught a bogus pass",
                classification="hallucinated_completion",
                source="guard",
                metadata={"reason_code": "hallucinated_completion"},
            )
        ],
    )
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([engine]),
        InMemorySessionStore(),
        retry_budget=3,
    )

    event = node.run(make_state())

    assert isinstance(event, Reject)
    assert event.source == "guard"
    assert event.classification == "hallucinated_completion"
    assert event.metadata["verdict_classification"] == "hallucinated_completion"
    assert event.metadata["reason_code"] == "hallucinated_completion"


def test_transient_error_not_in_retry_on_switches_engine_without_retry() -> None:
    codex = _ScriptedEngine("codex", [TransientError("service busy", failure_kind="service")])
    claude = _ScriptedEngine("claude", [AgentVerdict(outcome="pass")])
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([codex, claude]),
        InMemorySessionStore(),
        retry_budget=3,
        retry_on=("timeout",),
    )

    event = node.run(make_state())

    assert isinstance(event, Pass)
    assert codex.calls == 1
    assert claude.calls == 1


class _TimeoutThenPassManager:
    calls = 0
    last_kwargs: list[dict[str, object]] = []
    engine_name = "opencode"
    continuation = RuntimeEngineContinuation(session_id="opencode-session-123")

    def __init__(self, execution_root: Path, **kwargs: Any) -> None:
        del execution_root, kwargs

    def run(self, task, **kwargs) -> SubagentResult:
        del task
        _TimeoutThenPassManager.calls += 1
        _TimeoutThenPassManager.last_kwargs.append(dict(kwargs))
        if _TimeoutThenPassManager.calls == 1:
            return SubagentResult(
                ref=SubagentRef(
                    id="SA-0001",
                    role="swe",
                    engine=_TimeoutThenPassManager.engine_name,
                    status="failed",
                    path="subagents/SA-0001-swe",
                ),
                execution=_stub_execution(exit_code=124),
                execution_trace=ExecutionTrace.from_text(""),
                exit_code=124,
                failure=EngineFailure(
                    kind="retryable_execution_error",
                    reason="transient timeout",
                    classification="timeout",
                ),
                continuation=_TimeoutThenPassManager.continuation,
            )
        return SubagentResult(
            ref=SubagentRef(
                id="SA-0002",
                role="swe",
                engine=_TimeoutThenPassManager.engine_name,
                status="completed",
                path="subagents/SA-0002-swe",
            ),
            execution=_stub_execution(),
            execution_trace=ExecutionTrace.from_text(""),
            exit_code=0,
            continuation=_TimeoutThenPassManager.continuation,
        )


class _TimeoutThenNudgeThenPassManager:
    calls = 0
    last_kwargs: list[dict[str, object]] = []
    continuation = RuntimeEngineContinuation(thread_id="codex-thread-123")

    def __init__(self, execution_root: Path, **kwargs: Any) -> None:
        del execution_root, kwargs

    def run(self, task, **kwargs) -> SubagentResult:
        del task
        _TimeoutThenNudgeThenPassManager.calls += 1
        _TimeoutThenNudgeThenPassManager.last_kwargs.append(dict(kwargs))
        subagent_id = f"SA-000{_TimeoutThenNudgeThenPassManager.calls}"
        if _TimeoutThenNudgeThenPassManager.calls == 1:
            return SubagentResult(
                ref=SubagentRef(
                    id=subagent_id,
                    role="swe",
                    engine="codex",
                    status="failed",
                    path=f"subagents/{subagent_id}-swe",
                ),
                execution=_stub_execution(exit_code=124, stdout="timeout transcript"),
                execution_trace=ExecutionTrace.from_text("timeout transcript"),
                exit_code=124,
                failure=EngineFailure(
                    kind="retryable_execution_error",
                    reason="transient timeout",
                    classification="timeout",
                ),
                continuation=_TimeoutThenNudgeThenPassManager.continuation,
            )
        return SubagentResult(
            ref=SubagentRef(
                id=subagent_id,
                role="swe",
                engine="codex",
                status="completed",
                path=f"subagents/{subagent_id}-swe",
            ),
            execution=_stub_execution(stdout=f"attempt {_TimeoutThenNudgeThenPassManager.calls}"),
            execution_trace=ExecutionTrace.from_text(f"attempt {_TimeoutThenNudgeThenPassManager.calls}"),
            exit_code=0,
            continuation=_TimeoutThenNudgeThenPassManager.continuation,
        )


@pytest.fixture(autouse=True)
def _reset_timeout_then_pass_manager_state() -> None:
    _TimeoutThenPassManager.calls = 0
    _TimeoutThenPassManager.last_kwargs = []
    _TimeoutThenPassManager.engine_name = "opencode"
    _TimeoutThenPassManager.continuation = RuntimeEngineContinuation(session_id="opencode-session-123")
    _TimeoutThenNudgeThenPassManager.calls = 0
    _TimeoutThenNudgeThenPassManager.last_kwargs = []
    _TimeoutThenNudgeThenPassManager.continuation = RuntimeEngineContinuation(thread_id="codex-thread-123")


@pytest.mark.parametrize(
    ("engine_name", "continuation", "resume_id"),
    [
        ("codex", RuntimeEngineContinuation(thread_id="codex-thread-123"), "codex-thread-123"),
        ("claude", RuntimeEngineContinuation(session_id="claude-session-123"), "claude-session-123"),
        ("opencode", RuntimeEngineContinuation(session_id="opencode-session-123"), "opencode-session-123"),
        ("goz", RuntimeEngineContinuation(session_id="goz-session-123"), "goz-session-123"),
        ("gemini", RuntimeEngineContinuation(session_id="gemini-session-123"), "gemini-session-123"),
    ],
)
def test_agent_node_retries_timeout_via_existing_retry_flow(
    tmp_path,
    monkeypatch,
    engine_name: str,
    continuation: RuntimeEngineContinuation,
    resume_id: str,
) -> None:
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title=f"{engine_name} timeout retry")
    adapter = HeruEngineAdapter(engine_name, workspace=workspace)
    store = InMemorySessionStore()
    node = _HeruPromptAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([adapter]),
        store,
        retry_budget=3,
    )

    _TimeoutThenPassManager.calls = 0
    _TimeoutThenPassManager.last_kwargs = []
    _TimeoutThenPassManager.engine_name = engine_name
    _TimeoutThenPassManager.continuation = continuation
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.SubagentManager",
        _TimeoutThenPassManager,
    )
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    event = node.run(make_state(task_id=task.id))

    assert isinstance(event, Pass)
    assert _TimeoutThenPassManager.calls == 2
    assert _TimeoutThenPassManager.last_kwargs[0]["resume_session_id"] is None
    assert _TimeoutThenPassManager.last_kwargs[1]["resume_session_id"] == resume_id

    session = store.get_or_create(task.id, PipelineState.IMPLEMENTING, engine_name)
    assert session.engine_session_id == resume_id


def test_agent_node_nudges_timeout_retry_with_existing_codex_thread_id(tmp_path, monkeypatch) -> None:
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="codex timeout nudge")
    adapter = HeruEngineAdapter("codex", workspace=workspace)
    store = InMemorySessionStore()
    node = _HeruPromptAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([adapter]),
        store,
        retry_budget=3,
    )

    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.SubagentManager",
        _TimeoutThenNudgeThenPassManager,
    )

    def latest_verdict_after_for_nudge(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        if _TimeoutThenNudgeThenPassManager.calls == 2:
            return None
        if _TimeoutThenNudgeThenPassManager.calls == 3:
            return AgentVerdict(
                outcome="pass",
                reason="nudged report",
                metadata={"parsed_from_call": 3},
            )
        raise AssertionError(f"unexpected verdict lookup after manager call {_TimeoutThenNudgeThenPassManager.calls}")

    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.latest_verdict_after",
        latest_verdict_after_for_nudge,
    )

    event = node.run(make_state(task_id=task.id))

    assert isinstance(event, Pass)
    assert event.metadata["parsed_from_call"] == 3
    assert _TimeoutThenNudgeThenPassManager.calls == 3
    assert _TimeoutThenNudgeThenPassManager.last_kwargs[0]["resume_session_id"] is None
    assert _TimeoutThenNudgeThenPassManager.last_kwargs[1]["resume_session_id"] == "codex-thread-123"
    assert _TimeoutThenNudgeThenPassManager.last_kwargs[2]["resume_session_id"] == "codex-thread-123"
    third_prompt = _TimeoutThenNudgeThenPassManager.last_kwargs[2]["prompt"]
    assert isinstance(third_prompt, str)
    assert "IMPORTANT: this is a nudge" in third_prompt

    session = store.get_or_create(task.id, PipelineState.IMPLEMENTING, "codex")
    assert session.engine_session_id == "codex-thread-123"


def test_nudge_budget_exhausted_returns_crash() -> None:
    engine = _ScriptedEngine("codex", [NudgeRequired("silent")] * 5)
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([engine]),
        InMemorySessionStore(),
        retry_budget=3,
    )
    event = node.run(make_state())
    assert isinstance(event, Crash)
    assert event.exc_type == "NudgeBudgetExhausted"
    assert "litehive agent report" in event.message
    # After first nudge the engine gets called a second time. If it nudges
    # again, budget is exhausted → crash. So we expect exactly 2 calls.
    assert engine.calls == 2


# ── unrecoverable escalation ────────────────────────────────────────────


def test_unrecoverable_error_becomes_crash_immediately() -> None:
    engine = _ScriptedEngine("codex", [UnrecoverableError("broken prompt")])
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([engine]),
        InMemorySessionStore(),
        retry_budget=3,
    )
    event = node.run(make_state())
    assert isinstance(event, Crash)
    assert event.exc_type == "UnrecoverableError"


# ── per-engine session isolation ────────────────────────────────────────


def test_engine_switch_uses_fresh_session_per_engine() -> None:
    """Verify the same session is reused across retries on one engine,
    but a fresh session is created when switching engines."""
    codex = _ScriptedEngine("codex", [TransientError("a", failure_kind="timeout")] * 3)
    claude = _ScriptedEngine("claude", [AgentVerdict(outcome="pass")])
    store = InMemorySessionStore()
    node = _TrivialAgent(
        PipelineState.IMPLEMENTING,
        _ListSelector([codex, claude]),
        store,
        retry_budget=3,
    )
    node.run(make_state())

    # codex retries reuse one session; switching to claude uses a different one.
    assert len(codex.session_ids) == 3
    assert len(set(codex.session_ids)) == 1
    assert len(claude.session_ids) == 1
    assert claude.session_ids[0] != codex.session_ids[0]
