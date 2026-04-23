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
import subprocess
from typing import Any

import pytest

import litehive.lifecycle.orchestration as orchestration
from litehive.lifecycle.journal import SqliteJournal
from litehive.lifecycle.registry import build_registry
from litehive.lifecycle.runner import StateMachineRunner
from litehive.roles.base import PromptContext
from litehive.lifecycle.nodes.hook import HookRunner
from litehive.lifecycle.nodes.system import CommitNode, StubCommitNode
from litehive.lifecycle.nodes.agent import AgentVerdict, Engine, TransientError
from litehive.lifecycle.nodes.system import MergeConflict
from litehive.lifecycle.persistence import Limits, SqlitePersistence
from litehive.lifecycle.sessions import InMemorySessionStore
from litehive.lifecycle.types import PipelineMode
from litehive.domain.recovery import RecoveryTrigger
from litehive.state.records import get_task, get_task_worktree_path
from litehive.worktree import resolve_recorded_worktree_path

from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.lifecycle.orchestration import run_task
from litehive.state.records import create_task

pytestmark = pytest.mark.integration


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

    def run(self, spec, state) -> None:
        del spec, state
        return None


class _RecoveringEngine(_PassEngine):
    """Emits verdicts driven by the target node name so the test can script per-stage outcomes."""

    def __init__(self, plan: dict[str, str]) -> None:
        self.plan = plan

    def run_turn(self, session: Any, prompt: Any, state: Any) -> AgentVerdict:
        session.turn_count += 1
        session.engine_session_id = f"stub-{state.task_id}-{state.stage}"
        outcome = self.plan.get(state.stage, "pass")
        if outcome == "resume":
            trigger = state.active_recovery_trigger
            origin_stage = (
                trigger.origin_stage if isinstance(trigger, RecoveryTrigger) else "implementing"
            )
            return AgentVerdict(outcome="resume", metadata={"target_stage": origin_stage or "implementing"})
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
    assert ("ready", "worktree_sync") in from_to_pairs
    assert ("worktree_sync", "before_grooming") in from_to_pairs
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


class _FlakyEngine:
    def __init__(self, failure_kind: str) -> None:
        self.name = "codex"
        self.failure_kind = failure_kind
        self.calls = 0

    def run_turn(self, session: Any, prompt: Any, state: Any) -> AgentVerdict:
        self.calls += 1
        if self.calls == 1:
            raise TransientError("transient failure", failure_kind=self.failure_kind)
        return AgentVerdict(outcome="pass")


def test_run_task_uses_workspace_retry_on_for_live_execution_retries(tmp_path: Path) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_preference=["codex"],
            default_retry_limit=2,
            retry_on=["timeout"],
        ),
    )
    task = create_task(tmp_path, title="Retry once on timeout", pipeline_mode="single")
    engine = _FlakyEngine("timeout")

    result = run_task(tmp_path, task, engine_factory=lambda _engine_name: engine)

    assert result.final_stage == "done"
    assert engine.calls == 2


class _WorktreeCommitEngine:
    name = "stub"

    def __init__(self, root: Path, *, fail_stage: str | None = None) -> None:
        self.root = root
        self.fail_stage = fail_stage
        self.observed_main_clean = False
        self.observed_worktree: Path | None = None

    def run_turn(self, session: Any, prompt: Any, state: Any) -> AgentVerdict:
        session.turn_count += 1
        session.engine_session_id = f"stub-{state.task_id}-{state.stage}"
        if state.stage == "implementing":
            task = get_task(self.root, state.task_id)
            assert task is not None
            worktree = resolve_recorded_worktree_path(self.root, get_task_worktree_path(task))
            assert worktree is not None and worktree.exists()
            self.observed_worktree = worktree
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
            )
            dirty_paths = [
                line[3:]
                for line in status.stdout.splitlines()
                if line.strip() and not line[3:].startswith(".litehive/")
            ]
            self.observed_main_clean = status.returncode == 0 and not dirty_paths
            feature_path = worktree / "feature.txt"
            if not feature_path.exists():
                feature_path.write_text("from worktree\n", encoding="utf-8")
            subprocess.run(["git", "add", "feature.txt"], cwd=worktree, check=True)
            feature_status = subprocess.run(
                ["git", "status", "--porcelain", "feature.txt"],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
            )
            if feature_status.stdout.strip():
                subprocess.run(["git", "commit", "-qm", "feature"], cwd=worktree, check=True)
        if self.fail_stage == state.stage:
            return AgentVerdict(outcome="reject", reason=f"fail at {state.stage}")
        return AgentVerdict(outcome="pass")


def _init_git_workspace(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)


def test_run_task_creates_worktree_and_merges_back_into_main(tmp_path: Path) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="codex", engine_preference=["codex"]),
    )
    _init_git_workspace(tmp_path)
    task = create_task(tmp_path, title="Worktree merge")
    engine = _WorktreeCommitEngine(tmp_path)

    result = run_task(tmp_path, task, engine_factory=lambda _engine_name: engine)
    refreshed = get_task(tmp_path, task.id)

    assert result.final_stage == "done"
    assert engine.observed_main_clean is True
    assert engine.observed_worktree is not None
    assert (tmp_path / "feature.txt").read_text(encoding="utf-8") == "from worktree\n"
    assert refreshed is not None
    assert get_task_worktree_path(refreshed) is None
    assert not engine.observed_worktree.exists()


def test_run_task_cleans_up_worktree_after_failed_terminal_state(tmp_path: Path) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="codex", engine_preference=["codex"]),
    )
    _init_git_workspace(tmp_path)
    task = create_task(tmp_path, title="Worktree failure")
    engine = _WorktreeCommitEngine(tmp_path, fail_stage="implementing")

    result = run_task(tmp_path, task, engine_factory=lambda _engine_name: engine)
    refreshed = get_task(tmp_path, task.id)

    assert result.final_stage == "failed"
    assert engine.observed_worktree is not None
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert get_task_worktree_path(refreshed) is None
    assert not engine.observed_worktree.exists()


class _AlreadyLandedCommitNode(CommitNode):
    def _merge_worktree(self, state) -> dict[str, object] | None:
        return {
            "commit_result": {
                "status": "reconciled_noop",
                "reason": "already_landed",
                "head_sha": "deadbeefcafebabe",
            }
        }


def test_run_task_records_already_landed_commit_reconciliation(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="codex", engine_preference=["codex"]),
    )
    task = create_task(tmp_path, title="Already landed reconcile", pipeline_mode="single")
    persistence = SqlitePersistence(tmp_path)
    state = persistence.initialize(task.id, pipeline_mode=PipelineMode.SINGLE)
    state.last_report.files_changed = 1
    persistence.save(state)

    monkeypatch.setattr(orchestration, "_build_commit_node", lambda root: _AlreadyLandedCommitNode())

    result = run_task(tmp_path, task, engine_factory=lambda _engine_name: _PassEngine())
    refreshed = get_task(tmp_path, task.id)

    assert result.final_stage == "done"
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == "deadbeefcafebabe"

    journal = (tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "journal.md").read_text(
        encoding="utf-8"
    )
    assert "patch already landed on main at deadbeefcafebabe" in journal


def test_run_task_honors_task_retry_limit_override_for_live_execution_retries(tmp_path: Path) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_preference=["codex"],
            default_retry_limit=1,
            retry_on=["timeout"],
        ),
    )
    task = create_task(
        tmp_path,
        title="Task override gets one retry",
        pipeline_mode="single",
        retry_limit=2,
    )
    engine = _FlakyEngine("timeout")

    result = run_task(tmp_path, task, engine_factory=lambda _engine_name: engine)

    assert result.final_stage == "done"
    assert engine.calls == 2


# ── recovery flow ────────────────────────────────────────────────────────


class _OneShotConflictCommit(CommitNode):
    """Commit node that raises MergeConflict on its first call, then passes."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def _merge_worktree(self, state) -> None:
        self.calls += 1
        if self.calls == 1:
            raise MergeConflict(["a.txt"])
        return None


def test_merge_conflict_routes_to_merge_agent_then_back_to_after_commit(
    workspace: Path,
) -> None:
    """commit → MergeConflictDetected → merge_resolving (MergeAgent Pass) → after_commit → done."""
    persistence = SqlitePersistence(workspace)
    journal = SqliteJournal(workspace)

    commit_node = _OneShotConflictCommit()
    registry = build_registry(
        selector=_FixedSelector(_PassEngine()),
        session_store=InMemorySessionStore(),
        hook_runner=_NoopHookRunner(),
        commit_node=commit_node,
    )
    runner = StateMachineRunner(registry, persistence, journal=journal)

    state = persistence.initialize("T-E2E-MERGE", pipeline_mode=PipelineMode.SINGLE)
    state.last_report.files_changed = 1
    persistence.save(state)

    final_state = runner.run_task("T-E2E-MERGE")

    assert final_state.stage == "done"

    transitions = journal.load_transitions("T-E2E-MERGE")
    event_types = [row["event_type"] for row in transitions]
    from_to_pairs = [(row["from_stage"], row["to_stage"]) for row in transitions]

    assert "MergeConflictDetected" in event_types
    assert ("commit", "merge_resolving") in from_to_pairs
    assert ("merge_resolving", "after_commit") in from_to_pairs


def test_reject_from_implementing_retries_then_fails(workspace: Path) -> None:
    """Implementing rejects until its retry budget is exhausted, then fails terminally."""
    persistence = SqlitePersistence(
        workspace,
        limits=Limits(stage_retry_limit=2),
    )
    journal = SqliteJournal(workspace)
    sessions = InMemorySessionStore()

    plan = {"implementing": "reject"}  # always reject; retries will exhaust
    registry = build_registry(
        selector=_FixedSelector(_RecoveringEngine(plan)),
        session_store=sessions,
        hook_runner=_NoopHookRunner(),
        commit_node=StubCommitNode(),
    )
    runner = StateMachineRunner(registry, persistence, journal=journal)
    persistence.initialize("T-E2E-RECOVER", pipeline_mode=PipelineMode.FULL)

    final_state = runner.run_task("T-E2E-RECOVER")

    assert final_state.stage == "failed"

    transitions = journal.load_transitions("T-E2E-RECOVER")
    implementing_rejects = [
        row
        for row in transitions
        if row["from_stage"] == "implementing" and row["event_type"] == "Reject"
    ]
    seen_recovering = any(row["to_stage"] == "recovering" for row in transitions)
    assert len(implementing_rejects) == 3
    assert implementing_rejects[-1]["to_stage"] == "failed"
    assert not seen_recovering
