"""Render subagent execution traces from canonical event and stream artifacts."""

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any

from heru.types import LiveEvent, LiveTimeline as LiveEventStream, UnifiedEvent
from pydantic import ValidationError

from litehive.agents.session_store import load_subagent_event_stream
from litehive.domain.runtime import RuntimeSubagentState, SubagentRef
from litehive.domain.task import TaskRecord
from litehive.tasks.paths import read_text_artifact, resolve_artifact_path, task_dir

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionTraceView:
    """Human-readable execution trace plus the artifact it was derived from."""

    text: str
    source: Path | str | None
    cached_final_snapshot: bool = False


def parse_unified_events(stdout: str) -> tuple[UnifiedEvent, ...]:
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
    return tuple(events)


def render_event_for_execution_trace(event: UnifiedEvent) -> str:
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


def render_execution_trace_from_events(events: tuple[UnifiedEvent, ...], *, stderr: str) -> str:
    parts = [rendered for event in events if (rendered := render_event_for_execution_trace(event))]
    if not parts:
        return f"[stderr]\n{stderr.strip()}" if stderr.strip() else ""
    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.strip()}")
    return "\n\n".join(parts)


def event_stream_from_events(
    events: tuple[UnifiedEvent, ...],
    *,
    engine_name: str,
    task_id: str | None = None,
    subagent_id: str | None = None,
) -> LiveEventStream | None:
    if not events:
        return None
    event_stream = LiveEventStream(engine=engine_name, task_id=task_id, subagent_id=subagent_id)
    event_stream.events = [LiveEvent.model_validate(event.model_dump(mode="python")) for event in events]
    event_stream.recompute_counts()
    return event_stream


def render_execution_trace_from_streams(engine_name: str, *, stdout: str, stderr: str) -> str:
    events = parse_unified_events(stdout)
    if events:
        return render_execution_trace_from_events(events, stderr=stderr)
    parts = [stdout.strip()]
    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.strip()}")
    return "\n\n".join(part for part in parts if part).strip()


def render_execution_trace_from_event_stream_payload(payload: dict[str, Any], *, stderr: str) -> str:
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
    return render_execution_trace_from_events(tuple(events), stderr=stderr)


def load_subagent_execution_trace(
    root: Path,
    task: TaskRecord,
    ref: SubagentRef | RuntimeSubagentState,
    *,
    active: bool,
    runtime_state: RuntimeSubagentState | None = None,
) -> ExecutionTraceView | None:
    """Load a readable execution trace without relying on live transcript state."""

    base = task_dir(root, task) / ref.path
    if not active and ref.status != "running":
        cached = resolve_artifact_path(base, "execution_trace.md")
        if cached is not None:
            return ExecutionTraceView(
                text=read_text_artifact(cached),
                source=cached,
                cached_final_snapshot=True,
            )

    stderr = _read_stream_artifact(base, "stderr", active=active)
    event_stream = load_subagent_event_stream(root, task.id, ref.id)
    event_trace = render_execution_trace_from_event_stream_payload(
        event_stream,
        stderr="" if stderr is None else stderr.text,
    )
    if event_trace:
        return ExecutionTraceView(text=event_trace, source="subagent_sessions:event_stream")

    stdout = _read_stream_artifact(base, "stdout", active=active)
    if stdout is not None or stderr is not None:
        trace = render_execution_trace_from_streams(
            ref.engine,
            stdout="" if stdout is None else stdout.text,
            stderr="" if stderr is None else stderr.text,
        )
        source = None if stdout is None else stdout.source
        if source is None and stderr is not None:
            source = stderr.source
        return ExecutionTraceView(text=trace, source=source)

    snippet = "" if runtime_state is None else runtime_state.execution_trace_snippet.strip()
    if snippet:
        return ExecutionTraceView(text=snippet, source="runtime:execution_trace_snippet")
    return None


def _read_stream_artifact(base: Path, stream: str, *, active: bool) -> ExecutionTraceView | None:
    if stream not in {"stdout", "stderr"}:
        raise ValueError(f"Unsupported stream artifact: {stream}")
    names = (f"{stream}.log", f"{stream}.txt") if active else (f"{stream}.txt", f"{stream}.log")
    for name in names:
        path = resolve_artifact_path(base, name)
        if path is not None:
            return ExecutionTraceView(text=read_text_artifact(path), source=path)
    return None
