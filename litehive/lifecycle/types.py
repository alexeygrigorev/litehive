from enum import Enum

from litehive.domain.common import StringEnum


class NodeType(str, Enum):
    AGENT = "agent"
    HOOK = "hook"
    SYSTEM = "system"
    TERMINAL = "terminal"


class PipelineMode(str, Enum):
    FULL = "full"
    SINGLE = "single"


NodeName = str

AGENT_STAGES: tuple[NodeName, ...] = (
    "grooming",
    "implementing",
    "testing",
    "accepting",
)
SYSTEM_STAGES: tuple[NodeName, ...] = ("commit",)
STAGES: tuple[NodeName, ...] = AGENT_STAGES + SYSTEM_STAGES


def before(stage: NodeName) -> NodeName:
    return f"before_{stage}"


def after(stage: NodeName) -> NodeName:
    return f"after_{stage}"


STAGE_PHASES: tuple[NodeName, ...] = (
    "before_grooming",
    "grooming",
    "after_grooming",
    "before_implementing",
    "implementing",
    "after_implementing",
    "before_testing",
    "testing",
    "after_testing",
    "before_accepting",
    "accepting",
    "after_accepting",
    "commit",
    "after_commit",
)


def pipeline_stage_for_phase(phase: NodeName) -> NodeName:
    if phase == "recovering":
        return "grooming"
    if phase in STAGES:
        return phase
    if phase.startswith("before_"):
        candidate = phase.removeprefix("before_")
        if candidate in STAGES:
            return candidate
    if phase.startswith("after_"):
        candidate = phase.removeprefix("after_")
        if candidate in STAGES:
            return candidate
    return phase

ANY_STAGE_PHASE: frozenset[NodeName] = frozenset(STAGE_PHASES)

TERMINAL_NODES: frozenset[NodeName] = frozenset({"done", "failed"})

PRE_EXEC_NODE: NodeName = "recovering_pre_exec"
RECOVERING: NodeName = "recovering"
READY: NodeName = "ready"


class FailedReason(StringEnum):
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    RECOVERY_BUDGET_HIT = "recovery_budget_hit"
    RECOVERY_CRASHED = "recovery_crashed"
    RECOVERY_MISSING_TARGET_STAGE = "recovery_missing_target_stage"
    PRE_EXEC_RECOVERY_FAILED = "pre_exec_recovery_failed"
    OPERATOR_ABANDONED = "operator_abandoned"
    UNRECOVERABLE_ERROR = "unrecoverable_error"
