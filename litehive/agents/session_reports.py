"""Typed report payloads for persisted subagent snapshots."""

from dataclasses import dataclass, field
from typing import Mapping

from litehive.agents.sandbox import SandboxPolicySummary
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
    summary: str
    files_changed: list[str] = field(default_factory=list)
    tests: Mapping[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    resource_control: SandboxPolicySummary = field(default_factory=lambda: SandboxPolicySummary(enabled=False))
    interruption_reason: str | None = None
    continuation: Mapping[str, object] | None = None

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
        if self.continuation is None:
            return None
        return dict(self.continuation)
