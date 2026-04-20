"""Compatibility helpers for Litehive's external heru dependency."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable

from pydantic import ValidationError

from heru import (
    extract_engine_continuation as _extract_engine_continuation_from_adapter,
    extract_engine_timeline as _extract_engine_timeline_from_adapter,
    get_engine,
)
from heru.base import CLIExecutionResult, LATEST_CONTINUATION_SENTINEL, iter_jsonl_payloads
from heru.quota import UsageStatus, UsageWindow
from heru.types import LiveEvent, LiveTimeline, RuntimeEngineContinuation, UnifiedEvent


@dataclass(frozen=True, slots=True)
class UnifiedExecutionView:
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
    if not _looks_like_unified_jsonl(stdout):
        return None
    events: list[UnifiedEvent] = []
    for payload in iter_jsonl_payloads(stdout):
        try:
            event = UnifiedEvent.model_validate(payload)
        except ValidationError:
            continue
        events.append(event)
    if not events:
        return None
    return UnifiedExecutionView(tuple(events))


def render_execution_transcript(
    engine_name: str,
    execution: CLIExecutionResult | None,
    *,
    fallback_renderer: Callable[[CLIExecutionResult], str] | None = None,
) -> str:
    if execution is None:
        return ""
    unified = parse_unified_execution(execution.stdout)
    if unified is not None:
        return unified.transcript(stderr=execution.stderr)
    renderer = fallback_renderer or get_engine(engine_name).render_transcript
    return renderer(execution)


def resume_safe_model_override(
    engine_name: str,
    model_name: str | None,
    *,
    resume_session_id: str | None,
) -> str | None:
    if resume_session_id and engine_name == "opencode":
        return None
    return model_name


def extract_engine_timeline(
    engine_name: str,
    stdout: str,
    *,
    task_id: str | None = None,
    subagent_id: str | None = None,
) -> LiveTimeline | None:
    unified = parse_unified_execution(stdout)
    if unified is not None:
        return unified.timeline(engine_name=engine_name, task_id=task_id, subagent_id=subagent_id)
    return _extract_engine_timeline_from_adapter(
        engine_name,
        stdout,
        task_id=task_id,
        subagent_id=subagent_id,
    )


def extract_engine_continuation(
    engine_name: str,
    execution: CLIExecutionResult | None,
) -> RuntimeEngineContinuation | None:
    if execution is None:
        return None
    unified = parse_unified_execution(execution.stdout)
    if unified is not None:
        continuation = unified.continuation()
        if continuation is not None:
            return continuation
    return _extract_engine_continuation_from_adapter(engine_name, execution)


def resolve_engine_resume_session_id(
    engine_name: str,
    continuation: RuntimeEngineContinuation | str | None,
    *,
    prefer_latest: bool = False,
) -> str | None:
    if isinstance(continuation, str):
        return continuation or None
    if continuation is not None and continuation.resume_id:
        return continuation.resume_id
    if prefer_latest and get_engine(engine_name).supports_continue_latest():
        return LATEST_CONTINUATION_SENTINEL
    return None


def quota_short_term(status: UsageStatus) -> UsageWindow:
    window = getattr(status, "short_term", None)
    return window if isinstance(window, UsageWindow) else UsageWindow()


def quota_long_term(status: UsageStatus) -> UsageWindow:
    window = getattr(status, "long_term", None)
    return window if isinstance(window, UsageWindow) else UsageWindow()


def preferred_reset_at(
    status: UsageStatus,
    *,
    include_short_term_fallback: bool = False,
) -> str | None:
    long_term = quota_long_term(status)
    short_term = quota_short_term(status)
    return long_term.reset_at or (short_term.reset_at if include_short_term_fallback else None)


def usage_limit_block_reason(engine_name: str, status: UsageStatus) -> str | None:
    if status.error is not None or not status.limit_reached:
        return None

    short_term = quota_short_term(status)
    long_term = quota_long_term(status)

    if engine_name == "codex":
        used_percent = max(short_term.used_percent, long_term.used_percent)
        reset_at = preferred_reset_at(status)
        reset_suffix = f", resets at {reset_at}" if reset_at else ""
        return f"codex quota exhausted (used {used_percent:.0f}%{reset_suffix})"

    if engine_name == "claude":
        use_short_term = short_term.used_percent >= 80.0 and short_term.used_percent >= long_term.used_percent
        window_name = "5h" if use_short_term else "7d"
        window = short_term if use_short_term else long_term
        reset_suffix = f", resets {window.reset_at}" if window.reset_at else ""
        return f"claude usage limit reached ({window_name} window at {window.used_percent:.0f}%{reset_suffix})"

    if engine_name == "copilot":
        premium_remaining = getattr(status, "premium_remaining", None)
        premium_entitlement = getattr(status, "premium_entitlement", None)
        remaining_percent = long_term.percent_remaining
        reset_at = preferred_reset_at(status, include_short_term_fallback=True)
        reset_suffix = f", resets {reset_at}" if reset_at else ""
        if premium_remaining is not None and premium_entitlement is not None:
            return (
                "copilot premium requests low "
                f"({premium_remaining}/{premium_entitlement} remaining, {remaining_percent:.0f}%{reset_suffix})"
            )
        return f"copilot premium requests low ({remaining_percent:.0f}% remaining{reset_suffix})"

    if engine_name in {"goz", "opencode"}:
        used_percent = max(short_term.used_percent, long_term.used_percent)
        return f"zai usage limit reached ({used_percent:.0f}% used)"

    return None


def _looks_like_unified_jsonl(stdout: str) -> bool:
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return False
        return isinstance(payload, dict) and "kind" in payload
    return False


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
