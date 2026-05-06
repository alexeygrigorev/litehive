from enum import Enum

from litehive.domain.common import (
    PipelineMode as PipelineMode,
    PipelineState,
    StringEnum,
    canonical_pipeline_state,
)


class NodeType(str, Enum):
    AGENT = "agent"
    HOOK = "hook"
    SYSTEM = "system"
    TERMINAL = "terminal"


AGENT_STAGES: tuple[PipelineState, ...] = (
    PipelineState.GROOMING,
    PipelineState.IMPLEMENTING,
    PipelineState.TESTING,
    PipelineState.ACCEPTING,
)
SYSTEM_STAGES: tuple[PipelineState, ...] = (PipelineState.COMMIT,)
STAGES: tuple[PipelineState, ...] = AGENT_STAGES + SYSTEM_STAGES


def before(stage: str | PipelineState) -> PipelineState:
    """
    Return the pre-stage hook phase for an agent stage.

    Encodes the ``before_<stage>`` naming convention shared by the rule
    table and the prompt builder; defining it once here means a rename
    (or a new stage) only needs to update one place instead of every
    rule row that references the pre-hook.
    """
    return canonical_pipeline_state(f"before_{stage}")


def after(stage: str | PipelineState) -> PipelineState:
    """
    Return the post-stage hook phase for an agent stage.

    Mirror of :func:`before` for the post-stage hook half of the
    lifecycle; the rule table uses both helpers so the runner never has
    to format hook phase names by hand.
    """
    return canonical_pipeline_state(f"after_{stage}")


STAGE_PHASES: tuple[PipelineState, ...] = (
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


def pipeline_stage_for_phase(phase: str | PipelineState) -> PipelineState:
    """
    Collapse a hook/system phase to its primary agent stage.

    For example ``after_implementing → implementing``. Reporting and
    prompt scaffolding use this to attribute hook activity to the stage
    that owns it, so a hook reject at ``after_implementing`` shows up
    against the implementing stage rather than as its own row.
    """
    state = canonical_pipeline_state(phase)
    return _PRIMARY_STAGE_BY_PHASE.get(state, state)


ANY_STAGE_PHASE: frozenset[PipelineState] = frozenset(STAGE_PHASES)

TERMINAL_NODES: frozenset[PipelineState] = frozenset({PipelineState.DONE, PipelineState.FAILED})

PRE_EXEC_NODE: PipelineState = PipelineState.RECOVERING_PRE_EXEC
RECOVERING: PipelineState = PipelineState.RECOVERING
READY: PipelineState = PipelineState.READY


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
