"""Shared literals and helpers for litehive models."""

from datetime import UTC, datetime
from typing import Literal


TaskMode = Literal["tasks", "implementation"]
PipelineMode = Literal["single", "full"]
TaskComplexity = Literal["simple", "moderate", "complex"]
PlannedEffort = Literal["xs", "s", "m", "l", "xl"]
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
]
SubagentStatus = Literal["created", "running", "completed", "failed", "blocked", "interrupted"]
RunnerExecutionStatus = Literal["idle", "running", "late", "stale"]
OutcomeKind = Literal[
    "flagged",
    "blocked",
    "interrupted",
    "cancelled",
    "wont_do",
    "deferred",
    "duplicate",
]
RetrySource = Literal["global", "task"]
HumanCheckpoint = Literal["before_acceptance", "before_commit"]
UpstreamContributionKind = Literal[
    "runtime_bug",
    "missing_feature",
    "config_improvement",
    "prompt_improvement",
    "engine_adapter_fix",
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
EngineMonitoringSource = Literal["provider", "local"]
EngineLimitKind = Literal["quota", "rate", "budget", "capacity"]
LiveEventKind = Literal[
    "message",
    "tool_call",
    "tool_result",
    "error",
    "usage",
    "status",
]
LiveEventRole = Literal["assistant", "user", "system"]

FEEDBACK_CAP = 2000
_TRUNCATION_MARKER = "\n\n… [truncated — full transcript in subagent artifacts]"


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def cap_feedback(text: str, *, limit: int = FEEDBACK_CAP) -> str:
    """Truncate feedback to *limit* characters, appending a marker if trimmed."""
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
