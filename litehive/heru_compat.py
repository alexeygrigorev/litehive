"""Compatibility re-exports for older Litehive imports.

The implementation now lives in the standalone ``heru`` dependency.

The public heru surface has narrowed over time, so Litehive keeps the small
helper layer it still expects here instead of importing drifting symbols
directly from heru.
"""

from collections.abc import Callable

from heru import extract_engine_continuation, extract_engine_timeline, get_engine
from heru.base import CLIExecutionResult, LATEST_CONTINUATION_SENTINEL
from heru.quota import UsageStatus
from heru.types import RuntimeEngineContinuation


def quota_short_term(status: UsageStatus):
    return status.short_term


def quota_long_term(status: UsageStatus):
    return status.long_term


def preferred_reset_at(
    status: UsageStatus,
    *,
    include_short_term_fallback: bool = False,
) -> str | None:
    if status.long_term.reset_at:
        return status.long_term.reset_at
    if include_short_term_fallback:
        return status.short_term.reset_at
    return None


def usage_limit_block_reason(engine_name: str, status: UsageStatus) -> str | None:
    del engine_name
    if status.error is not None or not status.limit_reached:
        return None
    return "usage limit reached"


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
    if prefer_latest and engine_name == "claude":
        return LATEST_CONTINUATION_SENTINEL
    return None


def resume_safe_model_override(
    engine_name: str,
    model_name: str | None,
    *,
    resume_session_id: str | None = None,
) -> str | None:
    if resume_session_id and engine_name == "opencode":
        return None
    return model_name


def render_execution_transcript(
    engine_name: str,
    execution: CLIExecutionResult | None,
    *,
    fallback_renderer: Callable[[CLIExecutionResult], str] | None = None,
) -> str:
    if execution is None:
        return ""
    timeline = extract_engine_timeline(engine_name, execution.stdout)
    if timeline is not None:
        parts: list[str] = []
        for event in timeline.events:
            if event.kind in {"message", "status"} and event.content:
                parts.append(event.content)
            elif event.kind == "tool_result":
                if event.tool_output:
                    parts.append(event.tool_output)
                elif event.content:
                    parts.append(event.content)
        transcript = "\n\n".join(part for part in parts if part).strip()
        if transcript:
            return transcript
    if fallback_renderer is not None:
        return fallback_renderer(execution)
    return get_engine(engine_name).render_transcript(execution)


__all__ = [
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
