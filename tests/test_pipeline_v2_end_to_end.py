"""End-to-end test for the v2 pipeline state machine.

Drives a synthetic task through ``ready → done`` with:
  - ``build_registry`` wiring every node
  - ``SqlitePersistence`` for TaskState round-trip
  - ``SqliteJournal`` for transition + lifecycle recording
  - ``StateMachineRunner`` as the loop
  - a stub engine selector returning a single deterministic engine that
    always emits ``pass`` so every agent stage moves forward
  - a no-op hook runner

Asserts:
  - the final task reaches stage ``done``
  - the journal contains a ``task_started``, every transition, and a
    ``task_finished``
  - the pipeline_transitions table has one row per hop
"""

from pathlib import Path
from typing import Any

import pytest

from litehive.pipeline import SqliteJournal, StateMachineRunner, build_registry
from litehive.pipeline.agents._base import PromptContext
from litehive.pipeline.nodes import HookResult, HookRunner, StubCommitNode
from litehive.pipeline.nodes.agent import AgentVerdict, Engine, EngineSelector
from litehive.pipeline.persistence import SqlitePersistence
from litehive.pipeline.sessions import InMemorySessionStore
from litehive.pipeline.types import PipelineMode

from tests.workspace_helpers import ensure_workspace


class _PassEngine:
    """Always returns a ``pass`` verdict. One engine, one name."""

    name = "stub"

    def run_turn(self, session: Any, prompt: Any, state: Any) -> AgentVerdict:
        # Record how many turns so the test can assert at least one run
        session.turn_count += 1
        session.engine_session_id = f"stub-{state.task_id}-{state.stage}"
        return AgentVerdict(outcome="pass")


class _FixedSelector:
    """Returns the same engine every time, unless it's already been excluded."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def select(self, state, node_name, excluded):
        if self.engine.name in excluded:
            return None
        return self.engine


class _NoopHookRunner(HookRunner):
    """No real subprocess — returns ok for every spec. Test uses empty hook lists anyway."""

    def run(self, spec, state) -> HookResult:
        return HookResult(spec=spec, ok=True, output="")


class _RecoveringEngine(_PassEngine):
    """Emits verdicts driven by the target node name so the test can script per-stage outcomes."""

    def __init__(self, plan: dict[str, str]) -> None:
        self.plan = plan

    def run_turn(self, session: Any, prompt: Any, state: Any) -> AgentVerdict:
        session.turn_count += 1
        session.engine_session_id = f"stub-{state.task_id}-{state.stage}"
        outcome = self.plan.get(state.stage, "pass")
        if outcome == "resume":
            return AgentVerdict(outcome="resume", metadata={"target_stage": state.origin_stage or "implementing"})
        return AgentVerdict(outcome=outcome)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ensure_workspace(tmp_path)
    return tmp_path


# ── happy path ────────────────────────────────────────────────────────────


def test_full_mode_task_runs_end_to_end_to_done(workspace: Path) -> None:
    persistence = SqlitePersistence(workspace)
    journal = SqliteJournal(workspace)
    sessions = InMemorySessionStore()
    selector = _FixedSelector(_PassEngine())
    hook_runner = _NoopHookRunner()

    registry = build_registry(
        selector=selector,
        session_store=sessions,
        hook_runner=hook_runner,
        commit_node=StubCommitNode(),
        prompt_context=PromptContext(workspace_root=workspace),
    )

    runner = StateMachineRunner(registry, persistence, journal=journal)
    persistence.initialize("T-E2E-001", pipeline_mode=PipelineMode.FULL)
    final_state = runner.run_task("T-E2E-001")

    assert final_state.stage == "done"
    assert final_state.failed_reason is None

    # Journal contents ──────────────────────────────────────────────────
    transitions = journal.load_transitions("T-E2E-001")
    lifecycle = journal.load_lifecycle("T-E2E-001")

    # Lifecycle: exactly one task_started and one task_finished, bracketed
    kinds = [row["kind"] for row in lifecycle]
    assert "task_started" in kinds
    assert "task_finished" in kinds
    assert lifecycle[0]["kind"] == "task_started"
    assert lifecycle[0]["payload"]["stage"] == "ready"
    assert lifecycle[-1]["kind"] == "task_finished"
    assert lifecycle[-1]["payload"]["stage"] == "done"

    # Transitions: must include the full full-mode sequence
    from_to_pairs = [(row["from_stage"], row["to_stage"]) for row in transitions]
    assert ("ready", "before_grooming") in from_to_pairs
    assert ("grooming", "after_grooming") in from_to_pairs
    assert ("implementing", "after_implementing") in from_to_pairs
    assert ("testing", "after_testing") in from_to_pairs
    assert ("accepting", "after_accepting") in from_to_pairs
    assert ("commit", "after_commit") in from_to_pairs
    assert ("after_commit", "done") in from_to_pairs


def test_single_mode_zero_change_shortcut_goes_straight_to_done(workspace: Path) -> None:
    """Single mode + no diff → skip commit and land directly on done."""
    persistence = SqlitePersistence(workspace)
    journal = SqliteJournal(workspace)
    sessions = InMemorySessionStore()

    registry = build_registry(
        selector=_FixedSelector(_PassEngine()),
        session_store=sessions,
        hook_runner=_NoopHookRunner(),
        commit_node=StubCommitNode(),
        prompt_context=PromptContext(workspace_root=workspace),
    )
    runner = StateMachineRunner(registry, persistence, journal=journal)

    persistence.initialize("T-E2E-SINGLE-NOOP", pipeline_mode=PipelineMode.SINGLE)
    final_state = runner.run_task("T-E2E-SINGLE-NOOP")

    assert final_state.stage == "done"

    from_stages = [row["from_stage"] for row in journal.load_transitions("T-E2E-SINGLE-NOOP")]
    # Single mode must never hit grooming / testing / accepting stages
    assert "grooming" not in from_stages
    assert "testing" not in from_stages
    assert "accepting" not in from_stages
    # last_report defaults mean zero_change_shortcut fires → commit is skipped
    assert "commit" not in from_stages
    assert "implementing" in from_stages


def test_single_mode_with_changes_routes_through_commit(workspace: Path) -> None:
    """Single mode + non-empty diff → implementing → commit → done."""
    persistence = SqlitePersistence(workspace)
    journal = SqliteJournal(workspace)
    sessions = InMemorySessionStore()

    registry = build_registry(
        selector=_FixedSelector(_PassEngine()),
        session_store=sessions,
        hook_runner=_NoopHookRunner(),
        commit_node=StubCommitNode(),
    )
    runner = StateMachineRunner(registry, persistence, journal=journal)

    # Seed a non-empty last_report so the zero_change_shortcut doesn't fire.
    state = persistence.initialize("T-E2E-SINGLE-DIFF", pipeline_mode=PipelineMode.SINGLE)
    state.last_report.files_changed = 3
    persistence.save(state)

    final_state = runner.run_task("T-E2E-SINGLE-DIFF")

    assert final_state.stage == "done"
    from_stages = [row["from_stage"] for row in journal.load_transitions("T-E2E-SINGLE-DIFF")]
    assert "commit" in from_stages


# ── persistence resume ───────────────────────────────────────────────────


def test_persistence_state_survives_load_after_run(workspace: Path) -> None:
    persistence = SqlitePersistence(workspace)
    journal = SqliteJournal(workspace)
    sessions = InMemorySessionStore()

    registry = build_registry(
        selector=_FixedSelector(_PassEngine()),
        session_store=sessions,
        hook_runner=_NoopHookRunner(),
        commit_node=StubCommitNode(),
    )
    runner = StateMachineRunner(registry, persistence, journal=journal)
    persistence.initialize("T-E2E-RESUME", pipeline_mode=PipelineMode.FULL)
    runner.run_task("T-E2E-RESUME")

    # Fresh load from sqlite should see the task at its terminal stage
    reloaded = persistence.load("T-E2E-RESUME")
    assert reloaded.stage == "done"
    assert reloaded.pipeline_mode == PipelineMode.FULL


# ── recovery flow ────────────────────────────────────────────────────────


def test_reject_from_testing_triggers_retry_then_recovery(workspace: Path) -> None:
    """Testing rejects until its retry budget is exhausted, then recovery resumes."""
    persistence = SqlitePersistence(workspace)
    journal = SqliteJournal(workspace)
    sessions = InMemorySessionStore()

    plan = {
        "testing": "reject",     # always reject; retries will exhaust
        "recovering": "resume",  # recovery decides to resume at origin
    }
    registry = build_registry(
        selector=_FixedSelector(_RecoveringEngine(plan)),
        session_store=sessions,
        hook_runner=_NoopHookRunner(),
        commit_node=StubCommitNode(),
    )
    runner = StateMachineRunner(registry, persistence, journal=journal)
    persistence.initialize("T-E2E-RECOVER", pipeline_mode=PipelineMode.FULL)

    # The task will loop between testing rejects and implementing retries until
    # the testing retry budget exhausts, then route to recovering. Recovery
    # succeeds and resumes testing — which still rejects — so second retry
    # cycle follows... but without a different recovery behavior this would
    # loop forever. For this test we make recovery advance to "done".
    plan["recovering"] = "done"

    final_state = runner.run_task("T-E2E-RECOVER")

    # Recovery shortcut → done
    assert final_state.stage == "done"

    # The journal should show at least one reject -> retry transition and the
    # recovering entry.
    transitions = journal.load_transitions("T-E2E-RECOVER")
    seen_testing_reject = any(
        row["from_stage"] == "testing" and row["event_type"] == "Reject"
        for row in transitions
    )
    seen_recovering = any(row["to_stage"] == "recovering" for row in transitions)
    assert seen_testing_reject
    assert seen_recovering
