"""Litehive-side compatibility helpers for heru quota APIs."""

from __future__ import annotations

from heru import (
    render_execution_transcript,
    resolve_engine_resume_session_id,
    resume_safe_model_override,
)
from heru.base import LATEST_CONTINUATION_SENTINEL
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
