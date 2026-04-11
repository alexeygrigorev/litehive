from dataclasses import dataclass
from typing import Literal

from .types import NodeName


@dataclass(frozen=True)
class Event:
    pass


@dataclass(frozen=True)
class Pass(Event):
    """Agent or system node passed its stage."""


@dataclass(frozen=True)
class HookOk(Event):
    """All hooks in a phase ran without a rejecting failure."""


@dataclass(frozen=True)
class CleanState(Event):
    """Pre-execution checks found nothing unusual; proceed to pipeline entry."""


@dataclass(frozen=True)
class NeedsPreExecRecovery(Event):
    """Pre-execution checks found dirty state; route to pre-exec recovery."""


@dataclass(frozen=True)
class Reject(Event):
    source: Literal["agent", "hook", "guard", "system"]
    reason: str


@dataclass(frozen=True)
class Blocked(Event):
    reason: str


@dataclass(frozen=True)
class Crash(Event):
    exc_type: str
    message: str


@dataclass(frozen=True)
class Timeout(Event):
    pass


@dataclass(frozen=True)
class StageRetryLimitHit(Event):
    stage: NodeName


@dataclass(frozen=True)
class OverallRetryLimitHit(Event):
    pass


@dataclass(frozen=True)
class RecoverySucceeded(Event):
    resume: NodeName | Literal["done"]


@dataclass(frozen=True)
class RecoveryFailed(Event):
    reason: str


@dataclass(frozen=True)
class RecoveryBudgetHit(Event):
    pass


@dataclass(frozen=True)
class PreExecRecoverySucceeded(Event):
    resume_stage: NodeName


@dataclass(frozen=True)
class PreExecRecoveryFailed(Event):
    reason: str


@dataclass(frozen=True)
class PreExecRecoveryBudgetHit(Event):
    pass
