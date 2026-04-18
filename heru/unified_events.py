"""Helpers for heru's public unified JSONL event contract."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable

from pydantic import ValidationError

from heru.base import CLIExecutionResult, iter_jsonl_payloads
from heru.types import LiveEvent, LiveTimeline, RuntimeEngineContinuation, UnifiedEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UnifiedExecutionView:
    """Parsed heru unified stdout for a single execution."""

    events: tuple[UnifiedEvent, ...]

    def continuation(self) -> RuntimeEngineContinuation | None:
        continuation_id: str | None = None
        for event in self.events:
            if event.kind != "continuation":
                continue
            continuation_id = event.continuation_id or event.content or continuation_id
        if not continuation_id:
            return None
        return RuntimeEngineContinuation(session_id=continuation_id)

    def timeline(
        self,
        *,
        engine_name: str,
        task_id: str | None = None,
        subagent_id: str | None = None,
    ) -> LiveTimeline:
        timeline = LiveTimeline(engine=engine_name, task_id=task_id, subagent_id=subagent_id)
        timeline.events = [LiveEvent.model_validate(event.model_dump(mode="python")) for event in self.events]
        timeline.recompute_counts()
        return timeline

    def transcript(self, *, stderr: str) -> str:
        parts: list[str] = []
        for event in self.events:
            rendered = _render_event_for_transcript(event)
            if rendered:
                parts.append(rendered)
        if not parts:
            return f"[stderr]\n{stderr.strip()}" if stderr.strip() else ""
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.strip()}")
        return "\n\n".join(parts)


def parse_unified_execution(stdout: str) -> UnifiedExecutionView | None:
    events: list[UnifiedEvent] = []
    for payload in iter_jsonl_payloads(stdout):
        try:
            event = UnifiedEvent.model_validate(payload)
        except ValidationError as exc:
            logger.warning("Discarding invalid unified event payload %r: %s", payload, exc)
            continue
        events.append(event)
    if not events:
        return None
    return UnifiedExecutionView(tuple(events))


def render_execution_transcript(
    execution: CLIExecutionResult | None,
    *,
    fallback_renderer: Callable[[CLIExecutionResult], str] | None = None,
) -> str:
    if execution is None:
        return ""
    unified = parse_unified_execution(execution.stdout)
    if unified is not None:
        return unified.transcript(stderr=execution.stderr)
    if fallback_renderer is not None:
        return fallback_renderer(execution)
    return execution.transcript


def _render_event_for_transcript(event: UnifiedEvent) -> str:
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


__all__ = ["UnifiedExecutionView", "parse_unified_execution", "render_execution_transcript"]
