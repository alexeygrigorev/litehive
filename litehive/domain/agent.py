"""Subagent result models and exceptions."""

from dataclasses import dataclass

from heru.base import CLIExecutionResult
from heru.types import RuntimeEngineContinuation
from heru.types import SubagentRef


@dataclass(slots=True)
class EngineFailure:
    """Details about a subagent engine failure.

    Captures the specifics of why a subagent failed, including
    classification for recovery decisions and resource limit context.
    """

    kind: str  # Type of failure
    reason: str  # Human-readable failure reason
    classification: str | None = None  # Failure category for recovery routing


@dataclass(slots=True)
class SubagentResult:
    """Complete result of a subagent execution.

    Contains all the information about what happened during subagent
    execution, including success/failure details and context for
    potential continuation or recovery.
    """

    ref: SubagentRef  # Reference to the subagent that ran
    execution: CLIExecutionResult | None  # Low-level execution results
    execution_trace: str  # Rendered subagent execution trace
    exit_code: int  # Process exit code
    failure: EngineFailure | None = None  # Failure details if subagent failed
    continuation: RuntimeEngineContinuation | None = None  # Context for resuming if interrupted


class SubagentInactivityTimeout(RuntimeError):
    """Raised when a live subagent stops producing stdout for too long."""

    def __init__(self, execution: CLIExecutionResult, idle_seconds: float, limit_seconds: float) -> None:
        """Capture the live execution context plus the timing that crossed the inactivity threshold so the runner can attribute the kill reason in its outcome record."""
        self.execution = execution
        self.idle_seconds = idle_seconds
        self.limit_seconds = limit_seconds
        super().__init__(
            f"litehive killed stale subagent after {limit_seconds:g}s without new stdout (idle {idle_seconds:.1f}s)"
        )
