"""Typed report payloads for persisted subagent snapshots."""

from dataclasses import dataclass, field
from typing import Mapping

from litehive.sandbox.launcher import SandboxPolicySummary
from litehive.agents.session_continuation import NoSubagentContinuation, SubagentContinuationState
from litehive.domain.common import SubagentStatus


@dataclass(frozen=True, slots=True)
class SubagentReportPayload:
    """
    Structured report slice persisted inside ``subagent_sessions``.

    ``SubagentManager`` and ``SubagentSessionManager`` build this object
    while session storage still serializes to JSON-compatible dicts at
    the database boundary. Keeping the construction typed prevents the
    snapshot path from passing around broad ad hoc dictionaries while
    preserving the current persisted shape for existing readers.
    """

    status: SubagentStatus
    """Terminal or in-progress lifecycle status of the subagent."""

    summary: str
    """Free-text summary of what the subagent accomplished."""

    files_changed: list[str] = field(default_factory=list)
    """Normalized list of file paths the subagent modified."""

    tests: Mapping[str, object] = field(default_factory=dict)
    """Structured test summary with counts like added and passing."""

    warnings: list[str] = field(default_factory=list)
    """Collected warnings from the agent report and callback bookkeeping."""

    resource_control: SandboxPolicySummary = field(default_factory=lambda: SandboxPolicySummary(enabled=False))
    """Sandbox/resource policy summary attached to this report."""

    interruption_reason: str | None = None
    """Reason the process was interrupted, if applicable."""

    continuation: SubagentContinuationState = field(default_factory=NoSubagentContinuation)
    """Engine continuation state for multi-turn resume."""

    def as_dict(self) -> dict[str, object]:
        """
        Serialize to the JSON-compatible shape stored in SQLite.
        """
        return {
            "status": self.status.value,
            "summary": self.summary,
            "files_changed": list(self.files_changed),
            "tests": dict(self.tests),
            "warnings": list(self.warnings),
            "resource_control": self.resource_control.as_dict(),
            "interruption_reason": self.interruption_reason,
            "continuation": self.continuation_payload(),
        }

    def continuation_payload(self) -> dict[str, object] | None:
        """
        Return a mutable copy of the continuation payload when present.
        """
        return self.continuation.payload()
