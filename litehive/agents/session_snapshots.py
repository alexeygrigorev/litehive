"""Typed session snapshot inputs for subagent persistence."""

from dataclasses import dataclass, field

from litehive.agents.sandbox import SandboxPolicySummary
from litehive.agents.session_continuation import NoSubagentContinuation, SubagentContinuationState
from litehive.agents.session_reports import SubagentReportPayload
from litehive.domain.common import SubagentStatus


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
    continuation: SubagentContinuationState = field(default_factory=NoSubagentContinuation)

    def continuation_payload(self) -> dict[str, object] | None:
        """
        Serialize the continuation token for session storage.
        """
        return self.continuation.payload()


@dataclass(frozen=True, slots=True)
class RunningSubagentSessionMetadata:
    """
    Metadata-only update for a running subagent session.

    Used after the process pid is known and during live progress. Some
    engine progress callbacks can arrive before a pid is available, so
    the pid mirrors the nullable runtime row. It deliberately has no
    exit code or interruption reason because those belong to terminal
    snapshots, not running-session metadata.
    """

    pid: int | None
    continuation: SubagentContinuationState = field(default_factory=NoSubagentContinuation)

    def continuation_payload(self) -> dict[str, object] | None:
        """
        Serialize the continuation token for session storage.
        """
        return self.continuation.payload()


@dataclass(frozen=True, slots=True)
class SubagentSessionStorageFields:
    """
    Common persisted fields for one subagent session row.
    """

    id: str
    role: str
    engine: str
    status: SubagentStatus
    sandboxed: bool
    sandbox: str
    created_at: str
    updated_at: str
    resource_control: SandboxPolicySummary

    def as_dict(self) -> dict[str, object]:
        """
        Serialize common session fields for SQLite JSON storage.
        """
        return {
            "id": self.id,
            "role": self.role,
            "engine": self.engine,
            "status": self.status.value,
            "sandboxed": self.sandboxed,
            "sandbox": self.sandbox,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resource_control": self.resource_control.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RunningSubagentSessionRow:
    """
    Persisted session row while a subagent is still running.

    Running rows do not have terminal process fields. The PID can be
    absent because some adapter progress callbacks arrive before the
    adapter reports process start.
    """

    fields: SubagentSessionStorageFields
    pid: int | None
    continuation: SubagentContinuationState = field(default_factory=NoSubagentContinuation)

    def as_dict(self) -> dict[str, object]:
        """
        Serialize a running session row for SQLite JSON storage.
        """
        payload = self.fields.as_dict()
        payload.update(
            {
                "pid": self.pid,
                "exit_code": None,
                "interruption_reason": None,
                "continuation": self.continuation.payload(),
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class TerminalSubagentSessionRow:
    """
    Persisted session row after the engine process has exited.
    """

    fields: SubagentSessionStorageFields
    exit_code: int
    pid: int | None
    interruption_reason: str | None = None
    continuation: SubagentContinuationState = field(default_factory=NoSubagentContinuation)

    def as_dict(self) -> dict[str, object]:
        """
        Serialize a terminal session row for SQLite JSON storage.
        """
        payload = self.fields.as_dict()
        payload.update(
            {
                "pid": self.pid,
                "exit_code": self.exit_code,
                "interruption_reason": self.interruption_reason,
                "continuation": self.continuation.payload(),
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class InterruptedSubagentSessionRow:
    """
    Persisted session row for a paused subagent that may be resumed.

    An interrupted process may not have produced an exit code, so this
    row keeps interruption metadata separate from terminal completion.
    """

    fields: SubagentSessionStorageFields
    pid: int | None
    interruption_reason: str
    resume_stage: str
    exit_code: int | None = None
    continuation: SubagentContinuationState = field(default_factory=NoSubagentContinuation)

    def as_dict(self) -> dict[str, object]:
        """
        Serialize an interrupted session row for SQLite JSON storage.
        """
        payload = self.fields.as_dict()
        payload.update(
            {
                "pid": self.pid,
                "exit_code": self.exit_code,
                "interruption_reason": self.interruption_reason,
                "resume_stage": self.resume_stage,
                "continuation": self.continuation.payload(),
            }
        )
        return payload


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
