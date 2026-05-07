"""Typed continuation state for subagent session persistence."""

from dataclasses import dataclass
from typing import Protocol

from heru.types import RuntimeEngineContinuation


class SubagentContinuationState(Protocol):
    """
    Continuation state carried by report/session snapshot objects.
    """

    def payload(self) -> dict[str, object] | None:
        """
        Serialize continuation state for persisted JSON payloads.
        """


@dataclass(frozen=True, slots=True)
class NoSubagentContinuation:
    """
    Explicit state for a subagent turn without a continuation token.
    """

    def payload(self) -> dict[str, object] | None:
        """
        Serialize absence of continuation for persisted JSON payloads.
        """
        return None


@dataclass(frozen=True, slots=True)
class CapturedSubagentContinuation:
    """
    Explicit state for a subagent turn with a captured continuation token.
    """

    continuation: RuntimeEngineContinuation

    def payload(self) -> dict[str, object] | None:
        """
        Serialize the captured continuation token.
        """
        return self.continuation.model_dump(mode="python")


def subagent_continuation_state(
    continuation: RuntimeEngineContinuation | None,
) -> SubagentContinuationState:
    """
    Convert an optional Heru continuation into explicit subagent state.
    """
    if continuation is None:
        return NoSubagentContinuation()
    return CapturedSubagentContinuation(continuation)
