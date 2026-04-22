"""Litehive-side compatibility helpers for heru API drift.

Keep these wrappers thin and local so the rest of Litehive can depend on a
stable surface even when sibling heru refactors move or remove helpers.
"""

from __future__ import annotations

from heru import extract_engine_timeline, get_engine
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


def resolve_engine_resume_session_id(
    engine_name: str,
    continuation: RuntimeEngineContinuation | str | None,
    *,
    prefer_latest: bool = False,
) -> str | None:
    if continuation is None:
        return None
    if isinstance(continuation, RuntimeEngineContinuation):
        resume_id = continuation.resume_id
    else:
        resume_id = continuation
    if prefer_latest and engine_name == "claude":
        return LATEST_CONTINUATION_SENTINEL
    return resume_id


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
    fallback_renderer=None,
) -> str:
    if execution is None:
        return ""
    timeline = extract_engine_timeline(engine_name, execution.stdout)
    if timeline is not None and timeline.events:
        parts: list[str] = []
        for event in timeline.events:
            text = getattr(event, "text", "") or ""
            if text:
                parts.append(text)
        if parts:
            transcript = "\n\n".join(parts).strip()
            if transcript:
                return transcript
    if callable(fallback_renderer):
        return fallback_renderer(execution)
    return get_engine(engine_name).render_transcript(execution)


__all__ = [
    "LATEST_CONTINUATION_SENTINEL",
    "UsageStatus",
    "check_claude_quota",
    "check_codex_quota",
    "check_copilot_quota",
    "check_zai_quota",
    "preferred_reset_at",
    "quota_long_term",
    "quota_short_term",
    "render_execution_transcript",
    "resolve_engine_resume_session_id",
    "resume_safe_model_override",
    "usage_limit_block_reason",
]
