from enum import Enum
from typing import Literal


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


STAGE_PHASES: tuple[NodeName, ...] = tuple(
    phase for stage in STAGES for phase in (before(stage), stage, after(stage))
)

ANY_STAGE_PHASE: frozenset[NodeName] = frozenset(STAGE_PHASES)

TERMINAL_NODES: frozenset[NodeName] = frozenset({"done", "failed"})

PRE_EXEC_NODE: NodeName = "recovering_pre_exec"
RECOVERING: NodeName = "recovering"
READY: NodeName = "ready"


FailedReason = Literal[
    "recovery_exhausted",
    "recovery_budget_hit",
    "recovery_crashed",
    "pre_exec_recovery_failed",
    "operator_abandoned",
    "unrecoverable_error",
]
