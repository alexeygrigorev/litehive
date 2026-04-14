"""Shared literals and helpers for litehive models."""

from datetime import UTC, datetime
from typing import Literal

from heru.types import (
    EngineLimitKind,
    EngineMonitoringSource,
    LiveEventKind,
    LiveEventRole,
    SubagentStatus,
)


FEEDBACK_CAP = 2000
TRUNCATION_MARKER = "\n\n… [truncated — full transcript in subagent artifacts]"


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def cap_feedback(text: str, *, limit: int = FEEDBACK_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


# ── litehive-native task-lifecycle vocabularies ─────────────────────────────

OutcomeKind = Literal[
    "flagged",
    "blocked",
    "interrupted",
    "cancelled",
    "wont_do",
    "deferred",
    "duplicate",
]

OutcomeReasonCode = Literal[
    "verdict_fail",
    "verdict_reject",
    "verdict_blocked",
    "hallucinated_completion",
    "resource_limit",
    "missing_acceptance_criteria",
    "retry_limit_exhausted",
    "stage_retry_limit_exhausted",
    "execution_interrupted",
    "execution_cancelled",
    "stage_exception",
    "unsupported_verdict",
    "merge_conflict",
    "wont_do",
    "deferred",
    "duplicate",
]

PipelineMode = Literal["single", "full"]
TaskStatus = Literal[
    "queued",
    "in_progress",
    "interrupted",
    "parked",
    "done",
    "flagged",
    "merge_failed",
    "cancelled",
    "wont_do",
    "deferred",
    "duplicate",
]
PipelineStatus = Literal[
    "backlog",
    "grooming",
    "implementing",
    "testing",
    "accepting",
    "commit_to_git",
    "done",
    "merge_failed",
    "flagged",
]
RunnerExecutionStatus = Literal["idle", "running", "late", "stale"]


__all__ = [
    "EngineLimitKind",
    "EngineMonitoringSource",
    "FEEDBACK_CAP",
    "LiveEventKind",
    "LiveEventRole",
    "OutcomeKind",
    "OutcomeReasonCode",
    "PipelineMode",
    "PipelineStatus",
    "RunnerExecutionStatus",
    "SubagentStatus",
    "TaskStatus",
    "TRUNCATION_MARKER",
    "cap_feedback",
    "utcnow",
]
