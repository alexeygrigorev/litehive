"""Subagent result models and exceptions."""

from dataclasses import dataclass

from litehive.engines.base import CLIExecutionResult
from litehive.models import ResourceLimitEvent, SubagentRef


@dataclass(slots=True)
class EngineFailure:
    kind: str
    reason: str
    classification: str | None = None
    resource_limit_event: ResourceLimitEvent | None = None


@dataclass(slots=True)
class SubagentResult:
    ref: SubagentRef
    execution: CLIExecutionResult | None
    transcript: str
    exit_code: int
    failure: EngineFailure | None = None


class SubagentInactivityTimeout(RuntimeError):
    """Raised when a live subagent stops producing stdout for too long."""

    def __init__(
        self, execution: CLIExecutionResult, *, idle_seconds: float, limit_seconds: float
    ) -> None:
        self.execution = execution
        self.idle_seconds = idle_seconds
        self.limit_seconds = limit_seconds
        super().__init__(
            "litehive killed stale subagent after "
            f"{limit_seconds:g}s without new stdout (idle {idle_seconds:.1f}s)"
        )
