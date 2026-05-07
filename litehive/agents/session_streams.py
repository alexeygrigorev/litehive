"""Append-only stdout/stderr stream log tracking for subagent sessions."""

from dataclasses import dataclass, field
from pathlib import Path

from litehive.domain.runtime import Subagent
from litehive.observability.events import append_session_log, ensure_session_log


@dataclass
class SubagentStreamLog:
    """
    Track append-only stdout/stderr offsets for live subagent sessions.

    Snapshot writers rewrite the full stream artifacts on every
    callback; this collaborator owns the separate append-only log so
    progress callbacks never double-write stream chunks.
    """

    _offsets: dict[str, int] = field(default_factory=dict)

    def ensure(self, base: Path) -> None:
        """
        Ensure both append-only stream logs exist for a subagent directory.
        """
        ensure_session_log(base, "stdout")
        ensure_session_log(base, "stderr")

    def append_delta(self, base: Path, ref: Subagent, stream: str, full_content: str) -> None:
        """
        Append only the new portion of a stream to the append-only log.
        """
        key = f"{ref.id}:{stream}"
        previous_offset = self._offsets.get(key, 0)
        if len(full_content) <= previous_offset:
            return
        append_session_log(base, stream, full_content[previous_offset:])
        self._offsets[key] = len(full_content)
