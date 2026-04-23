"""Litehive-side compatibility helpers for current heru quota APIs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Callable

from pydantic import ValidationError

from heru import (
    extract_engine_continuation as _extract_engine_continuation_from_adapter,
    extract_engine_timeline as _extract_engine_timeline_from_adapter,
    get_engine,
)
from heru.base import CLIExecutionResult, LATEST_CONTINUATION_SENTINEL
from heru.quota import (
    UsageStatus,
    check_claude_quota,
    check_codex_quota,
    check_copilot_quota,
    check_zai_quota,
    claude_quota_block_reason,
    codex_quota_block_reason,
    copilot_quota_block_reason,
    zai_quota_block_reason,
)
from heru.types import LiveEvent, LiveTimeline, RuntimeEngineContinuation, UnifiedEvent

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class _UnifiedJsonlLine:
    line_number: int
    raw_line: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class _UnifiedParseWarning:
    message: str
    context: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _UnifiedJsonlScan:
    candidate_lines: tuple[_UnifiedJsonlLine, ...]
    warnings: tuple[_UnifiedParseWarning, ...]


def quota_short_term(status: UsageStatus):
    return status.short_term


def quota_long_term(status: UsageStatus):
    return status.long_term


def preferred_reset_at(
    status: UsageStatus,
    *,
    include_short_term_fallback: bool = False,
) -> str | None:
    long_term = quota_long_term(status)
    if long_term.reset_at:
        return long_term.reset_at
    if include_short_term_fallback:
        return quota_short_term(status).reset_at
    return None


def usage_limit_block_reason(engine_name: str, status: UsageStatus) -> str | None:
    if status.error:
        return None
    if engine_name == "codex":
        return codex_quota_block_reason()
    if engine_name == "claude":
        return claude_quota_block_reason()
    if engine_name == "copilot":
        return copilot_quota_block_reason()
    if engine_name in {"goz", "opencode"}:
        return zai_quota_block_reason()
    if status.limit_reached:
        reset_at = preferred_reset_at(status, include_short_term_fallback=True)
        reset_suffix = f", resets {reset_at}" if reset_at else ""
        return f"{engine_name} usage limit reached{reset_suffix}"
    return None


def parse_unified_execution(stdout: str) -> UnifiedExecutionView | None:
    scan = _collect_unified_jsonl_candidates(stdout)
    for warning in scan.warnings:
        logger.warning(warning.message, *warning.context)

    if not scan.candidate_lines:
        return None

    events: list[UnifiedEvent] = []
    for item_index, candidate in enumerate(scan.candidate_lines, 1):
        try:
            event = UnifiedEvent.model_validate(candidate.payload)
        except ValidationError as exc:
            logger.warning(
                "parse_unified_execution: skipping invalid unified event at line %d (item %d): %s (content: %.200s)",
                candidate.line_number,
                item_index,
                _summarize_validation_error(exc),
                candidate.raw_line,
            )
            continue
        events.append(event)
    if not events:
        logger.warning("parse_unified_execution: detected unified JSONL output but found no valid events")
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


def resume_safe_model_override(
    engine_name: str,
    model_name: str | None,
    *,
    resume_session_id: str | None,
) -> str | None:
    if resume_session_id and engine_name == "opencode":
        return None
    return model_name


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


def _summarize_validation_error(exc: ValidationError) -> str:
    details: list[str] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        message = error.get("msg", "validation error")
        details.append(f"{location}: {message}")
    return "; ".join(details) if details else str(exc)


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


def _collect_unified_jsonl_candidates(stdout: str) -> _UnifiedJsonlScan:
    candidates: list[_UnifiedJsonlLine] = []
    rejected_lines: list[_UnifiedParseWarning] = []
    native_lines: list[_UnifiedParseWarning] = []
    saw_jsonl_like_content = False

    for line_number, raw_line in enumerate(stdout.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        looks_like_jsonl = line.startswith("{") or line.startswith("[")
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            if looks_like_jsonl:
                saw_jsonl_like_content = True
            rejected_lines.append(
                _UnifiedParseWarning(
                    "parse_unified_execution: skipping unparseable JSONL line %d: %s (content: %.200s)",
                    (line_number, exc, line),
                )
            )
            continue
        saw_jsonl_like_content = True
        if not isinstance(payload, dict):
            rejected_lines.append(
                _UnifiedParseWarning(
                    "parse_unified_execution: skipping non-object JSONL line %d (type=%s, content: %.200s)",
                    (line_number, type(payload).__name__, line),
                )
            )
            continue
        if "kind" in payload:
            candidates.append(_UnifiedJsonlLine(line_number=line_number, raw_line=line, payload=payload))
            continue
        if _is_native_engine_payload(payload):
            native_lines.append(
                _UnifiedParseWarning(
                    "parse_unified_execution: skipping native engine payload at line %d while parsing unified output (content: %.200s)",
                    (line_number, line),
                )
            )
            continue
        rejected_lines.append(
            _UnifiedParseWarning(
                "parse_unified_execution: skipping JSON object without unified event kind at line %d (content: %.200s)",
                (line_number, line),
            )
        )
        continue

    warnings: list[_UnifiedParseWarning] = []
    if candidates:
        warnings.extend(rejected_lines)
        warnings.extend(native_lines)
    elif saw_jsonl_like_content and not native_lines:
        warnings.extend(rejected_lines)

    return _UnifiedJsonlScan(candidate_lines=tuple(candidates), warnings=tuple(warnings))


def _is_native_engine_payload(payload: dict[str, object]) -> bool:
    return "type" in payload or isinstance(payload.get("event"), dict)


__all__ = [
    "UsageStatus",
    "check_claude_quota",
    "check_codex_quota",
    "check_copilot_quota",
    "check_zai_quota",
    "extract_engine_continuation",
    "extract_engine_timeline",
    "preferred_reset_at",
    "quota_long_term",
    "quota_short_term",
    "render_execution_transcript",
    "resolve_engine_resume_session_id",
    "resume_safe_model_override",
    "usage_limit_block_reason",
]
