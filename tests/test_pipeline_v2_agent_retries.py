"""Unit tests for AgentNode retry / backoff / nudge / engine-switch paths.

These run AgentNode.run() directly with scripted engines so each branch is
exercised without the full state machine.
"""

from typing import Any

from litehive.pipeline.events import Crash, Pass
from litehive.pipeline.nodes.agent import (
    AgentNode,
    AgentVerdict,
    NudgeRequired,
    TransientError,
    UnrecoverableError,
)
from litehive.pipeline.persistence import TaskState
from litehive.pipeline.sessions import InMemorySessionStore
from litehive.pipeline.types import PipelineMode


class _ScriptedEngine:
    """Walks through a scripted sequence of outcomes on every ``run_turn``."""

    def __init__(self, name: str, script: list) -> None:
        self.name = name
        self.script = list(script)
        self.calls = 0

    def run_turn(self, session: Any, prompt: Any, state: TaskState) -> AgentVerdict:
        self.calls += 1
        # Track per-call what prompt was given so tests can inspect nudges
        session.metadata.setdefault("prompts_seen", []).append(prompt)
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
        "implementing",
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
        "implementing",
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
        "implementing",
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
        "implementing",
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
            NudgeRequired("no litehive report call"),
            AgentVerdict(outcome="pass"),
        ],
    )
    store = InMemorySessionStore()
    node = _TrivialAgent(
        "implementing",
        _ListSelector([engine]),
        store,
        retry_budget=3,
    )
    event = node.run(make_state())
    assert isinstance(event, Pass)
    assert engine.calls == 2
    # Verify the second call received a nudged prompt
    session = store.get_or_create("T-0001", "implementing", "codex")
    prompts = session.metadata["prompts_seen"]
    assert len(prompts) == 2
    assert "nudge" not in prompts[0] or not prompts[0].get("nudge")
    assert prompts[1]["nudge"] is True
    assert "nudge_message" in prompts[1]


def test_nudge_does_not_consume_retry_budget() -> None:
    """Nudge + retry should both work independently of each other's budgets."""
    engine = _ScriptedEngine(
        "codex",
        [
            NudgeRequired("missed"),              # nudge
            TransientError("blip", failure_kind="timeout"),  # tier-1 retry (1/3)
            TransientError("blip", failure_kind="timeout"),  # tier-1 retry (2/3)
            AgentVerdict(outcome="pass"),         # success
        ],
    )
    node = _TrivialAgent(
        "implementing",
        _ListSelector([engine]),
        InMemorySessionStore(),
        retry_budget=3,
    )
    event = node.run(make_state())
    assert isinstance(event, Pass)
    assert engine.calls == 4


def test_transient_error_not_in_retry_on_switches_engine_without_retry() -> None:
    codex = _ScriptedEngine("codex", [TransientError("service busy", failure_kind="service")])
    claude = _ScriptedEngine("claude", [AgentVerdict(outcome="pass")])
    node = _TrivialAgent(
        "implementing",
        _ListSelector([codex, claude]),
        InMemorySessionStore(),
        retry_budget=3,
        retry_on=("timeout",),
    )

    event = node.run(make_state())

    assert isinstance(event, Pass)
    assert codex.calls == 1
    assert claude.calls == 1


def test_nudge_budget_exhausted_returns_crash() -> None:
    engine = _ScriptedEngine("codex", [NudgeRequired("silent")] * 5)
    node = _TrivialAgent(
        "implementing",
        _ListSelector([engine]),
        InMemorySessionStore(),
        retry_budget=3,
    )
    event = node.run(make_state())
    assert isinstance(event, Crash)
    assert event.exc_type == "NudgeBudgetExhausted"
    # After first nudge the engine gets called a second time. If it nudges
    # again, budget is exhausted → crash. So we expect exactly 2 calls.
    assert engine.calls == 2


# ── unrecoverable escalation ────────────────────────────────────────────


def test_unrecoverable_error_becomes_crash_immediately() -> None:
    engine = _ScriptedEngine("codex", [UnrecoverableError("broken prompt")])
    node = _TrivialAgent(
        "implementing",
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
        "implementing",
        _ListSelector([codex, claude]),
        store,
        retry_budget=3,
    )
    node.run(make_state())

    # codex session got 3 entries; claude session got 1
    codex_session = store.get_or_create("T-0001", "implementing", "codex")
    claude_session = store.get_or_create("T-0001", "implementing", "claude")
    assert len(codex_session.metadata.get("prompts_seen", [])) == 3
    assert len(claude_session.metadata.get("prompts_seen", [])) == 1
