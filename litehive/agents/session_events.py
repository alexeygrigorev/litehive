"""Typed persisted events for subagent sessions."""

from dataclasses import dataclass
from typing import ClassVar, Mapping

from litehive.domain.common import SubagentStatus


@dataclass(frozen=True, slots=True)
class SubagentStartedEvent:
    """
    Persisted when a subagent session is allocated before engine launch.

    Used by recent-activity status views and event-log replay to show
    that the runner reserved a concrete subagent slot before process
    launch.
    """

    persistence_reason: ClassVar[str] = "record subagent slot allocation before engine launch"
    consumed_by: ClassVar[tuple[str, ...]] = ("status recent activity", "event-log replay")

    subagent_id: str
    role: str
    engine: str
    sandboxed: bool

    @property
    def kind(self) -> str:
        """Event kind stored in the task event stream."""
        return "subagent_started"

    def data(self) -> Mapping[str, object]:
        """Serialize event-specific fields for storage."""
        return {
            "subagent_id": self.subagent_id,
            "role": self.role,
            "engine": self.engine,
            "sandboxed": self.sandboxed,
        }


@dataclass(frozen=True, slots=True)
class SubagentPidEvent:
    """
    Persisted when the runner learns the external engine process pid.

    Used by recent-activity status views and event-log replay to show
    when the launched subagent became tied to a live OS process.
    """

    persistence_reason: ClassVar[str] = "record the live engine process pid for a subagent"
    consumed_by: ClassVar[tuple[str, ...]] = ("status recent activity", "event-log replay")

    subagent_id: str
    role: str
    pid: int

    @property
    def kind(self) -> str:
        """Event kind stored in the task event stream."""
        return "subagent_pid"

    def data(self) -> Mapping[str, object]:
        """Serialize event-specific fields for storage."""
        return {"subagent_id": self.subagent_id, "role": self.role, "pid": self.pid}


@dataclass(frozen=True, slots=True)
class SubagentProgressEvent:
    """
    Persisted when a live progress snapshot is written.

    Used by recent-activity status views and event-log replay to show
    that the running subagent produced updated observable output.
    """

    persistence_reason: ClassVar[str] = "record that a live subagent progress snapshot was persisted"
    consumed_by: ClassVar[tuple[str, ...]] = ("status recent activity", "event-log replay")

    subagent_id: str
    role: str
    pid: int | None

    @property
    def kind(self) -> str:
        """Event kind stored in the task event stream."""
        return "subagent_progress"

    def data(self) -> Mapping[str, object]:
        """Serialize event-specific fields for storage."""
        return {"subagent_id": self.subagent_id, "role": self.role, "pid": self.pid}


@dataclass(frozen=True, slots=True)
class SubagentFinishedEvent:
    """
    Persisted when a subagent reaches terminal session persistence.

    Used by recent-activity status views and event-log replay to show
    the final subagent outcome after report/session artifacts are
    durable.
    """

    persistence_reason: ClassVar[str] = "record terminal subagent outcome after artifacts are durable"
    consumed_by: ClassVar[tuple[str, ...]] = ("status recent activity", "event-log replay")

    subagent_id: str
    role: str
    engine: str
    status: SubagentStatus
    exit_code: int
    interruption_reason: str | None

    @property
    def kind(self) -> str:
        """Event kind stored in the task event stream."""
        return "subagent_finished"

    def data(self) -> Mapping[str, object]:
        """Serialize event-specific fields for storage."""
        return {
            "subagent_id": self.subagent_id,
            "role": self.role,
            "engine": self.engine,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "interruption_reason": self.interruption_reason,
        }
