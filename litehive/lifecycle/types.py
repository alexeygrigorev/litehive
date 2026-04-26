from enum import Enum

from litehive.domain.common import PipelineState, StringEnum, canonical_pipeline_state


class NodeType(str, Enum):
    AGENT = "agent"
    HOOK = "hook"
    SYSTEM = "system"
    TERMINAL = "terminal"


class PipelineMode(str, Enum):
    FULL = "full"
    SINGLE = "single"


NodeName = PipelineState

AGENT_STAGES: tuple[NodeName, ...] = (
    PipelineState.GROOMING,
    PipelineState.IMPLEMENTING,
    PipelineState.TESTING,
    PipelineState.ACCEPTING,
)
SYSTEM_STAGES: tuple[NodeName, ...] = (PipelineState.COMMIT,)
STAGES: tuple[NodeName, ...] = AGENT_STAGES + SYSTEM_STAGES


def before(stage: str | NodeName) -> NodeName:
    return canonical_pipeline_state(f"before_{stage}")


def after(stage: str | NodeName) -> NodeName:
    return canonical_pipeline_state(f"after_{stage}")


STAGE_PHASES: tuple[NodeName, ...] = (
    PipelineState.BEFORE_GROOMING,
    PipelineState.GROOMING,
    PipelineState.AFTER_GROOMING,
    PipelineState.BEFORE_IMPLEMENTING,
    PipelineState.IMPLEMENTING,
    PipelineState.AFTER_IMPLEMENTING,
    PipelineState.BEFORE_TESTING,
    PipelineState.TESTING,
    PipelineState.AFTER_TESTING,
    PipelineState.BEFORE_ACCEPTING,
    PipelineState.ACCEPTING,
    PipelineState.AFTER_ACCEPTING,
    PipelineState.COMMIT,
    PipelineState.AFTER_COMMIT,
)


_PRIMARY_STAGE_BY_PHASE: dict[PipelineState, PipelineState] = {
    PipelineState.BEFORE_GROOMING: PipelineState.GROOMING,
    PipelineState.AFTER_GROOMING: PipelineState.GROOMING,
    PipelineState.BEFORE_IMPLEMENTING: PipelineState.IMPLEMENTING,
    PipelineState.AFTER_IMPLEMENTING: PipelineState.IMPLEMENTING,
    PipelineState.BEFORE_TESTING: PipelineState.TESTING,
    PipelineState.AFTER_TESTING: PipelineState.TESTING,
    PipelineState.BEFORE_ACCEPTING: PipelineState.ACCEPTING,
    PipelineState.AFTER_ACCEPTING: PipelineState.ACCEPTING,
    PipelineState.AFTER_COMMIT: PipelineState.COMMIT,
    PipelineState.RECOVERING: PipelineState.GROOMING,
}


def pipeline_stage_for_phase(phase: str | NodeName) -> NodeName:
    state = canonical_pipeline_state(phase)
    return _PRIMARY_STAGE_BY_PHASE.get(state, state)


ANY_STAGE_PHASE: frozenset[NodeName] = frozenset(STAGE_PHASES)

TERMINAL_NODES: frozenset[NodeName] = frozenset({PipelineState.DONE, PipelineState.FAILED})

PRE_EXEC_NODE: NodeName = PipelineState.RECOVERING_PRE_EXEC
RECOVERING: NodeName = PipelineState.RECOVERING
READY: NodeName = PipelineState.READY


class FailedReason(StringEnum):
    HOOK_REJECT_LOOP = "hook_reject_loop"
    REJECTION_LOOP_DETECTED = "rejection_loop_detected"
    SEMANTIC_REJECT = "semantic_reject"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    RECOVERY_BUDGET_HIT = "recovery_budget_hit"
    RECOVERY_CRASHED = "recovery_crashed"
    RECOVERY_MISSING_TARGET_STAGE = "recovery_missing_target_stage"
    PRE_EXEC_RECOVERY_FAILED = "pre_exec_recovery_failed"
    OPERATOR_ABANDONED = "operator_abandoned"
    TIME_BUDGET_EXCEEDED = "time_budget_exceeded"
    UNRECOVERABLE_ERROR = "unrecoverable_error"
