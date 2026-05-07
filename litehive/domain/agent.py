"""
Subagent execution results and the inactivity-timeout exception.

The runner uses ``SubagentResult`` to carry every aspect of a single
subagent run (refs, exit code, optional ``EngineFailure`` classification,
optional continuation token) back to the lifecycle layer; the lifecycle
layer reads it without knowing the heru transport details. Lives in
``domain`` so prompt builders and reporting can quote the same shapes
without importing engine internals.
"""

from dataclasses import dataclass
from typing import NewType

from heru.base import CLIExecutionResult
from heru.types import RuntimeEngineContinuation

from litehive.domain.runtime import Subagent


SubagentId = NewType("SubagentId", str)


@dataclass(slots=True)
class EngineFailure:
    """
    Engine-side failure description attached to a ``SubagentResult``.

    Recovery routing reads ``classification`` to decide whether the
    failure deserves another retry or counts as a hard stop, while
    ``kind`` and ``reason`` feed the operator-facing failure summary.
    Constructed by the runner when a subagent comes back non-zero or
    is killed by an engine limit.
    """

    kind: str  # Type of failure
    reason: str  # Human-readable failure reason
    classification: str | None = None  # Failure category for recovery routing


@dataclass(slots=True)
class SubagentResult:
    """
    Single-run summary the lifecycle layer reads back from the runner.

    Carries everything the lifecycle and recovery layers need to advance
    the state machine: the persisted subagent record, the low-level
    execution handle, the rendered execution trace, the exit code, an
    optional ``EngineFailure`` for routing, and an optional continuation
    token so an interrupted run can resume. The runner builds one of
    these per subagent invocation; no other layer constructs them.
    """

    ref: Subagent  # Subagent record for the run
    execution: CLIExecutionResult | None  # Low-level execution results
    execution_trace: str  # Rendered subagent execution trace
    exit_code: int  # Process exit code
    failure: EngineFailure | None = None  # Failure details if subagent failed
    continuation: RuntimeEngineContinuation | None = None  # Context for resuming if interrupted


class SubagentInactivityTimeout(RuntimeError):
    """
    Raised when a live subagent stops producing stdout for too long.

    The runner watches subagent stdout and kills the process when it
    goes silent past the configured threshold; this exception carries
    the timing facts so the resulting failure record can distinguish
    "engine was idle" from "agent crashed" in the operator report.
    """

    def __init__(self, execution: CLIExecutionResult, idle_seconds: float, limit_seconds: float) -> None:
        """
        Capture the live execution handle plus the threshold crossing.

        The runner attributes the kill reason from these fields when
        building the terminal ``SubagentResult``; without the timings
        the operator report could only say "agent was killed" without
        explaining why.
        """
        self.execution = execution
        self.idle_seconds = idle_seconds
        self.limit_seconds = limit_seconds
        super().__init__(
            f"litehive killed stale subagent after {limit_seconds:g}s without new stdout (idle {idle_seconds:.1f}s)"
        )
