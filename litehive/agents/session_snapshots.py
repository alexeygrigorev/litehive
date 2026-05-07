"""Typed session snapshot inputs for subagent persistence."""

from dataclasses import dataclass

from heru.types import RuntimeEngineContinuation

from litehive.agents.session_reports import SubagentReportPayload


@dataclass(frozen=True, slots=True)
class SubagentSessionMetadata:
    """
    Metadata slice stored with a subagent session row.

    Shared by metadata-only updates and full snapshots so callers pass
    one named object instead of parallel nullable fields.
    """

    exit_code: int | None
    pid: int | None
    interruption_reason: str | None = None
    continuation: RuntimeEngineContinuation | None = None

    def continuation_payload(self) -> dict[str, object] | None:
        """
        Serialize the continuation token for session storage.
        """
        if self.continuation is None:
            return None
        return self.continuation.model_dump(mode="python")


@dataclass(frozen=True, slots=True)
class RunningSubagentSessionMetadata:
    """
    Metadata-only update for a running subagent session.

    Used after the process pid is known and during live progress. It
    deliberately has no exit code or interruption reason because those
    belong to terminal snapshots, not running-session metadata.
    """

    pid: int
    continuation: RuntimeEngineContinuation | None = None

    def continuation_payload(self) -> dict[str, object] | None:
        """
        Serialize the continuation token for session storage.
        """
        if self.continuation is None:
            return None
        return self.continuation.model_dump(mode="python")


@dataclass(frozen=True, slots=True)
class SubagentSessionSnapshot:
    """
    Complete subagent snapshot written by ``SubagentSessionManager``.
    """

    prompt: str
    transcript: str
    stdout: str
    stderr: str
    report: SubagentReportPayload
    metadata: SubagentSessionMetadata
