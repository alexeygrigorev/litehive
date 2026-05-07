"""Typed persisted events for subagent sessions."""

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class SubagentStartedEvent:
    """
    Persisted when a subagent session is allocated before engine launch.
    """

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
    """

    subagent_id: str
    pid: int

    @property
    def kind(self) -> str:
        """Event kind stored in the task event stream."""
        return "subagent_pid"

    def data(self) -> Mapping[str, object]:
        """Serialize event-specific fields for storage."""
        return {"subagent_id": self.subagent_id, "pid": self.pid}


@dataclass(frozen=True, slots=True)
class SubagentProgressEvent:
    """
    Persisted when a live progress snapshot is written.
    """

    subagent_id: str
    pid: int | None

    @property
    def kind(self) -> str:
        """Event kind stored in the task event stream."""
        return "subagent_progress"

    def data(self) -> Mapping[str, object]:
        """Serialize event-specific fields for storage."""
        return {"subagent_id": self.subagent_id, "pid": self.pid}


@dataclass(frozen=True, slots=True)
class SubagentFinishedEvent:
    """
    Persisted when a subagent reaches terminal session persistence.
    """

    subagent_id: str
    role: str
    engine: str
    status: str
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
            "status": self.status,
            "exit_code": self.exit_code,
            "interruption_reason": self.interruption_reason,
        }
