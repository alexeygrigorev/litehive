"""Render subagent execution traces from canonical event and stream artifacts."""

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any

from heru.base import CLIExecutionResult
from heru.types import LiveEvent, LiveTimeline, UnifiedEvent
from pydantic import ValidationError

from litehive.agents.session_store import subagent_artifacts
from litehive.domain.common import SubagentStatus
from litehive.domain.runtime import RuntimeSubagentState, Subagent
from litehive.domain.task import TaskRecord
from litehive.tasks.paths import read_text_artifact, resolve_artifact_path
from litehive.workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionTraceView:
    """Human-readable execution trace plus the artifact it was derived from."""

    text: str
    source: Path | str | None
    cached_final_snapshot: bool = False


@dataclass(frozen=True, slots=True)
class ParsedUnifiedEvents:
    """
    Parsed unified events recovered from an engine stdout buffer.

    Execution-trace rendering and event-stream reconstruction use this
    object when the structured session-store event stream is missing.
    The named wrapper makes it clear that callers are handling a
    recovered timeline, not an arbitrary tuple of events.
    """

    events: tuple[UnifiedEvent, ...]

    def __bool__(self) -> bool:
        return bool(self.events)


class ExecutionTraceRenderer:
    """
    Render and load subagent execution traces from structured events and artifacts.
    """

    def parse_unified_events(self, stdout: str) -> ParsedUnifiedEvents:
        events: list[UnifiedEvent] = []
        for line_number, raw_line in enumerate(stdout.splitlines(), 1):
            line = raw_line.strip()
            if not line:
                continue
            if not (line.startswith("{") or line.startswith("[")):
                continue
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(payload, dict) or "kind" not in payload:
                continue
            try:
                events.append(UnifiedEvent.model_validate(payload))
            except ValidationError as exc:
                logger.warning(
                    "Skipping invalid unified event line %d while parsing subagent output: %s",
                    line_number,
                    exc,
                )
        return ParsedUnifiedEvents(events=tuple(events))

    def render_event(self, event: UnifiedEvent) -> str:
        if event.kind in {"message", "status"} and event.content:
            return event.content
        if event.kind == "error" and event.error:
            return event.error
        if event.kind not in {"tool_call", "tool_result"}:
            return ""

        lines = ["```tool"]
        if event.tool_name:
            lines.append(f"name: {event.tool_name}")
        if event.tool_input:
            lines.append("input:")
            lines.append(event.tool_input.rstrip())
        if event.tool_output:
            lines.append("output:")
            lines.append(event.tool_output.rstrip())
        if event.error:
            lines.append("error:")
            lines.append(event.error.rstrip())
        lines.append("```")
        return "\n".join(lines)

    def render_from_events(self, events: ParsedUnifiedEvents, stderr: str = "") -> str:
        parts = [rendered for event in events.events if (rendered := self.render_event(event))]
        if not parts:
            if stderr.strip():
                return f"[stderr]\n{stderr.strip()}"
            return ""
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.strip()}")
        return "\n\n".join(parts)

    def render(self, execution: CLIExecutionResult) -> str:
        events = self.parse_unified_events(execution.stdout)
        if not events:
            return execution.transcript
        return self.render_from_events(events, stderr=execution.stderr)

    def recovered_timeline_from_events(
        self,
        events: ParsedUnifiedEvents,
        engine_name: str,
        task_id: str | None = None,
        subagent_id: str | None = None,
    ) -> LiveTimeline | None:
        if not events:
            return None
        event_stream = LiveTimeline(engine=engine_name, task_id=task_id, subagent_id=subagent_id)
        event_stream.events = _rehydrate_live_events(events.events)
        event_stream.recompute_counts()
        return event_stream

    def render_from_streams(self, stdout: str, stderr: str) -> str:
        events = self.parse_unified_events(stdout)
        if events:
            return self.render_from_events(events, stderr=stderr)
        parts = [stdout.strip()]
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.strip()}")
        return "\n\n".join(part for part in parts if part).strip()

    def render_from_payload(self, payload: dict[str, Any], stderr: str = "") -> str:
        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            return ""
        events: list[UnifiedEvent] = []
        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                continue
            try:
                events.append(UnifiedEvent.model_validate(raw_event))
            except ValidationError:
                continue
        return self.render_from_events(ParsedUnifiedEvents(events=tuple(events)), stderr=stderr)

    def load_for_subagent(
        self,
        workspace: Workspace,
        task: TaskRecord,
        ref: Subagent | RuntimeSubagentState,
        active: bool,
        runtime_state: RuntimeSubagentState | None = None,
    ) -> ExecutionTraceView | None:
        base = workspace.task_dir(task) / ref.path
        stderr = self._read_stream_artifact(base, "stderr", active=active)
        if stderr is None:
            stderr_text = ""
        else:
            stderr_text = stderr.text
        event_stream = subagent_artifacts(workspace, task.id, ref.id).load_event_stream()
        event_trace = self.render_from_payload(
            event_stream,
            stderr=stderr_text,
        )
        if event_trace:
            return ExecutionTraceView(text=event_trace, source="subagent_sessions:event_stream")

        if not active and ref.status != SubagentStatus.RUNNING:
            cached = resolve_artifact_path(base, "execution_trace.md")
            if cached is not None:
                return ExecutionTraceView(
                    text=read_text_artifact(cached),
                    source=cached,
                    cached_final_snapshot=True,
                )

        stdout = self._read_stream_artifact(base, "stdout", active=active)
        if stdout is None:
            stdout_text = ""
        else:
            stdout_text = stdout.text
        if stdout is not None or stderr is not None:
            trace = self.render_from_streams(
                stdout=stdout_text,
                stderr=stderr_text,
            )
            if stdout is None:
                source = None
            else:
                source = stdout.source
            if source is None and stderr is not None:
                source = stderr.source
            return ExecutionTraceView(text=trace, source=source)

        if runtime_state is None:
            snippet = ""
        else:
            snippet = runtime_state.execution_trace_snippet.strip()
        if snippet:
            return ExecutionTraceView(text=snippet, source="runtime:execution_trace_snippet")
        return None

    def _read_stream_artifact(self, base: Path, stream: str, active: bool) -> ExecutionTraceView | None:
        if stream not in {"stdout", "stderr"}:
            raise ValueError(f"Unsupported stream artifact: {stream}")
        if active:
            names = (f"{stream}.log", f"{stream}.txt")
        else:
            names = (f"{stream}.txt", f"{stream}.log")
        for name in names:
            path = resolve_artifact_path(base, name)
            if path is not None:
                return ExecutionTraceView(text=read_text_artifact(path), source=path)
        return None


def execution_trace_renderer() -> ExecutionTraceRenderer:
    """Return the default execution trace renderer."""
    return ExecutionTraceRenderer()


def _rehydrate_live_events(events) -> list[LiveEvent]:
    """
    Round-trip parsed events through ``LiveEvent`` validation.

    The fallback timeline assembled by `recovered_timeline_from_events`
    needs `LiveEvent` instances even when the parser produced a
    different concrete type, so each event is dumped to a plain dict
    and revalidated to land on the canonical pydantic shape.
    """
    rehydrated: list[LiveEvent] = []
    for event in events:
        payload = event.model_dump(mode="python")
        rehydrated.append(LiveEvent.model_validate(payload))
    return rehydrated
