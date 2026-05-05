"""Tests for ReadyNode probe injection + PreExecRecoveryNode repairs."""

from litehive.lifecycle.events import (
    CleanState,
    NeedsPreExecRecovery,
    PreExecRecoveryBudgetHit,
    PreExecRecoverySucceeded,
)
from litehive.lifecycle.nodes.system import PreExecRecoveryNode, ReadyNode
from litehive.domain.common import PipelineState
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.types import PipelineMode


def _state(**overrides) -> TaskState:
    return TaskState(
        task_id="T-0001",
        stage=PipelineState.READY,
        pipeline_mode=PipelineMode.FULL,
        **overrides,
    )


def test_ready_node_probe_triggers_needs_recovery() -> None:
    """A probe that returns True flips ReadyNode from CleanState to NeedsPreExecRecovery."""
    calls: list[str] = []

    def never_fires(state):
        calls.append("never")
        return False

    def always_fires(state):
        calls.append("always")
        return True

    # Clean when all probes return False.
    assert isinstance(ReadyNode(probes=[never_fires]).run(_state()), CleanState)

    # Triggers when any probe returns True.
    event = ReadyNode(probes=[never_fires, always_fires]).run(_state())
    assert isinstance(event, NeedsPreExecRecovery)
    assert calls == ["never", "never", "always"]  # both runs hit never_fires

    # A probe that raises is treated as "needs recovery" (safe default).
    def boom(state):
        raise RuntimeError("probe broke")

    assert isinstance(ReadyNode(probes=[boom]).run(_state()), NeedsPreExecRecovery)


def test_pre_exec_recovery_runs_repairs_then_succeeds() -> None:
    """PreExecRecoveryNode runs every repair then emits PreExecRecoverySucceeded
    — unless the budget was already consumed, in which case it emits
    PreExecRecoveryBudgetHit."""
    applied: list[str] = []

    def fix_a(state):
        applied.append("a")

    def fix_b(state):
        applied.append("b")

    def boom(state):
        raise RuntimeError("repair failed")

    # All three run; the boom repair is caught and logged.
    node = PreExecRecoveryNode(repairs=[fix_a, boom, fix_b])
    event = node.run(_state(pre_exec_recovery_attempt=1))
    assert isinstance(event, PreExecRecoverySucceeded)
    assert event.resume_stage == "grooming"
    assert applied == ["a", "b"]

    # Budget exhausted → short-circuit, no repairs run.
    applied.clear()
    budget_event = PreExecRecoveryNode(repairs=[fix_a]).run(_state(pre_exec_recovery_attempt=2))
    assert isinstance(budget_event, PreExecRecoveryBudgetHit)
    assert applied == []
