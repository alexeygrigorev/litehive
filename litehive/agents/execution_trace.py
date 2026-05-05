"""Render subagent execution traces from canonical event and stream artifacts."""

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any

from heru.types import LiveEvent, LiveTimeline, UnifiedEvent
from pydantic import ValidationError

from litehive.agents.session_store import load_subagent_event_stream
from litehive.domain.runtime import RuntimeSubagentState, SubagentRef
from litehive.domain.task import TaskRecord
from litehive.tasks.paths import read_text_artifact, resolve_artifact_path, task_dir
from litehive.workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionTraceView:
    """Human-readable execution trace plus the artifact it was derived from."""

    text: str
    source: Path | str | None
    cached_final_snapshot: bool = False


def parse_unified_events(stdout: str) -> tuple[UnifiedEvent, ...]:
    """
    Recover unified events from a captured engine stdout buffer.

    Used when the structured event stream artifact is unavailable —
    a partial run, a crash, an older format. Tolerant of garbage and
    non-JSON lines so partial captures still yield a usable trace
    rather than failing the whole parse on one malformed line.
    """
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
    """
    Format one unified event as a Markdown chunk for the trace.

    Tool calls and results are wrapped in fenced ``tool`` blocks so
    the trace round-trips cleanly through diff and chat UIs that
    only render fenced code; without the fence, free-form tool
    output would interfere with surrounding Markdown.
    """
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
    """Stitch rendered events plus any stderr tail into the canonical trace text written to ``execution_trace.md`` and shown by the status UI — the single place that decides how events and stderr compose."""
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
) -> LiveTimeline | None:
    """
    Wrap parsed unified events into a heru ``LiveTimeline``.

    Lets the timeline-renderer paths treat reconstructed-from-stdout
    traces (this function's input) and live captures uniformly — the
    renderer only knows how to read ``LiveTimeline``, so the
    fallback path has to lift its parsed events into that shape.
    """
    if not events:
        return None
    event_stream = LiveTimeline(engine=engine_name, task_id=task_id, subagent_id=subagent_id)
    event_stream.events = _rehydrate_live_events(events)
    event_stream.recompute_counts()
    return event_stream


def _rehydrate_live_events(events) -> list[LiveEvent]:
    """
    Round-trip parsed events through ``LiveEvent`` validation.

    The fallback timeline assembled by :func:`render_execution_trace_from_events`
    needs ``LiveEvent`` instances even when the parser produced a
    different concrete type, so each event is dumped to a plain dict
    and revalidated to land on the canonical pydantic shape.
    """
    rehydrated: list[LiveEvent] = []
    for event in events:
        payload = event.model_dump(mode="python")
        rehydrated.append(LiveEvent.model_validate(payload))
    return rehydrated


def render_execution_trace_from_streams(stdout: str, stderr: str) -> str:
    """
    Build a trace from raw stdout/stderr.

    Fallback path used by the loader when no structured event stream
    artifact survived (older runs, partial captures); produces a
    readable trace that lets diagnostics still recover *something*
    from a half-broken subagent run.
    """
    events = parse_unified_events(stdout)
    if events:
        return render_execution_trace_from_events(events, stderr=stderr)
    parts = [stdout.strip()]
    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.strip()}")
    return "\n\n".join(part for part in parts if part).strip()


def render_execution_trace_from_event_stream_payload(payload: dict[str, Any], stderr: str) -> str:
    """
    Render a trace from the persisted JSON event-stream payload.

    Preferred path because it reads from the session store rather
    than parsing engine stdout — engines do not always flush
    well-formed JSONL, but the session-store payload was written by
    our own code and is structurally clean.
    """
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
    """
    Load a readable execution trace for one subagent.

    Walks four sources in preference order — cached
    ``execution_trace.md`` (only for finished runs), persisted
    event-stream payload, raw stdout/stderr, and the runtime state
    snippet — so callers always get the freshest trace available
    without relying on a live transcript that may have been
    truncated or lost.
    """

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
    if stderr is None:
        stderr_text = ""
    else:
        stderr_text = stderr.text
    event_stream = load_subagent_event_stream(Workspace.from_path(root), task.id, ref.id)
    event_trace = render_execution_trace_from_event_stream_payload(
        event_stream,
        stderr=stderr_text,
    )
    if event_trace:
        return ExecutionTraceView(text=event_trace, source="subagent_sessions:event_stream")

    stdout = _read_stream_artifact(base, "stdout", active=active)
    if stdout is None:
        stdout_text = ""
    else:
        stdout_text = stdout.text
    if stdout is not None or stderr is not None:
        trace = render_execution_trace_from_streams(
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


def _read_stream_artifact(base: Path, stream: str, active: bool) -> ExecutionTraceView | None:
    """
    Pick the right stdout/stderr artifact file for the stream.

    While a subagent runs, the live ``.log`` is the freshest source;
    after it finishes, the persisted ``.txt`` is the canonical one.
    The order flips on ``active`` so we don't accidentally read a
    stale ``.txt`` over a still-growing ``.log`` and miss recent
    output.
    """
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
