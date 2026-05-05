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
    """Recover unified events from a captured engine stdout buffer when the structured event stream is unavailable; tolerant of garbage/non-JSON lines so partial captures still yield a usable trace."""
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
    """Format one unified event as the human-readable Markdown chunk that ends up in the persisted execution trace; tool calls and results are wrapped in fenced ``tool`` blocks so the trace round-trips through diff and chat UIs."""
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


def render_execution_trace_from_events(events: tuple[UnifiedEvent, ...], stderr: str) -> str:
    """Stitch rendered events plus any stderr tail into the canonical trace text written to ``execution_trace.md`` and shown by the status UI."""
    parts = [rendered for event in events if (rendered := render_event_for_execution_trace(event))]
    if not parts:
        if stderr.strip():
            return f"[stderr]\n{stderr.strip()}"
        return ""
    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.strip()}")
    return "\n\n".join(parts)


def event_stream_from_events(
    events: tuple[UnifiedEvent, ...],
    engine_name: str,
    task_id: str | None = None,
    subagent_id: str | None = None,
) -> LiveEventStream | None:
    """Wrap parsed unified events into a heru ``LiveEventStream`` so the timeline-renderer paths can treat reconstructed-from-stdout traces and live captures uniformly."""
    if not events:
        return None
    event_stream = LiveEventStream(engine=engine_name, task_id=task_id, subagent_id=subagent_id)
    event_stream.events = [LiveEvent.model_validate(event.model_dump(mode="python")) for event in events]
    event_stream.recompute_counts()
    return event_stream


def render_execution_trace_from_streams(engine_name: str, stdout: str, stderr: str) -> str:
    """Build a trace from raw stdout/stderr when no structured event stream artifact survived; fallback path used by the loader when the modern session-store events are missing."""
    events = parse_unified_events(stdout)
    if events:
        return render_execution_trace_from_events(events, stderr=stderr)
    parts = [stdout.strip()]
    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.strip()}")
    return "\n\n".join(part for part in parts if part).strip()


def render_execution_trace_from_event_stream_payload(payload: dict[str, Any], stderr: str) -> str:
    """Render a trace from the JSON event-stream payload persisted by the session store; preferred path because it does not depend on the engine flushing well-formed JSONL to stdout."""
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


def _read_stream_artifact(base: Path, stream: str, active: bool) -> ExecutionTraceView | None:
    """Pick the right stdout/stderr artifact file: the live ``.log`` while a subagent runs, the persisted ``.txt`` after it finishes; the order flips so we don't read a stale ``.txt`` over a still-growing ``.log``."""
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
