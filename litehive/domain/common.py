"""Shared enums and helpers for litehive models."""

from datetime import UTC, datetime
from enum import Enum

from heru.types import (
    EngineLimitKind,
    EngineMonitoringSource,
    LiveEventKind,
    LiveEventRole,
    SubagentStatus,
)


FEEDBACK_CAP = 2000
TRUNCATION_MARKER = "\n\n… [truncated — full transcript in subagent artifacts]"


class StringEnum(str, Enum):
    """Base class for string-valued enums used across persisted models."""

    def __str__(self) -> str:
        return self.value


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def cap_feedback(text: str, *, limit: int = FEEDBACK_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


# ── litehive-native task-lifecycle vocabularies ─────────────────────────────


class OutcomeKind(StringEnum):
    FLAGGED = "flagged"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    WONT_DO = "wont_do"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"


class OutcomeReasonCode(StringEnum):
    VERDICT_FAIL = "verdict_fail"
    VERDICT_REJECT = "verdict_reject"
    VERDICT_BLOCKED = "verdict_blocked"
    HALLUCINATED_COMPLETION = "hallucinated_completion"
    RESOURCE_LIMIT = "resource_limit"
    MISSING_ACCEPTANCE_CRITERIA = "missing_acceptance_criteria"
    RETRY_LIMIT_EXHAUSTED = "retry_limit_exhausted"
    STAGE_RETRY_LIMIT_EXHAUSTED = "stage_retry_limit_exhausted"
    EXECUTION_INTERRUPTED = "execution_interrupted"
    EXECUTION_CANCELLED = "execution_cancelled"
    STAGE_EXCEPTION = "stage_exception"
    UNSUPPORTED_VERDICT = "unsupported_verdict"
    MERGE_CONFLICT = "merge_conflict"
    WONT_DO = "wont_do"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"


class PipelineMode(StringEnum):
    SINGLE = "single"
    FULL = "full"


class TaskStage(StringEnum):
    GROOMING = "grooming"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    ACCEPTING = "accepting"
    COMMIT_TO_GIT = "commit_to_git"


class LifecyclePhase(StringEnum):
    READY = "ready"
    WORKTREE_SYNC = "worktree_sync"
    RECOVERING_PRE_EXEC = "recovering_pre_exec"
    BEFORE_GROOMING = "before_grooming"
    GROOMING = "grooming"
    AFTER_GROOMING = "after_grooming"
    BEFORE_IMPLEMENTING = "before_implementing"
    IMPLEMENTING = "implementing"
    AFTER_IMPLEMENTING = "after_implementing"
    BEFORE_TESTING = "before_testing"
    TESTING = "testing"
    AFTER_TESTING = "after_testing"
    BEFORE_ACCEPTING = "before_accepting"
    ACCEPTING = "accepting"
    AFTER_ACCEPTING = "after_accepting"
    BEFORE_COMMIT = "before_commit"
    COMMIT = "commit"
    AFTER_COMMIT = "after_commit"
    MERGE_RESOLVING = "merge_resolving"
    RECOVERING = "recovering"
    DONE = "done"
    FAILED = "failed"


class TaskStatus(StringEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    INTERRUPTED = "interrupted"
    PARKED = "parked"
    DONE = "done"
    FLAGGED = "flagged"
    MERGE_FAILED = "merge_failed"
    CANCELLED = "cancelled"
    WONT_DO = "wont_do"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"


class PipelineStatus(StringEnum):
    BACKLOG = "backlog"
    GROOMING = "grooming"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    ACCEPTING = "accepting"
    COMMIT_TO_GIT = "commit_to_git"
    DONE = "done"
    MERGE_FAILED = "merge_failed"
    FLAGGED = "flagged"


class RunnerStatus(StringEnum):
    IDLE = "idle"
    RUNNING = "running"
    LATE = "late"
    STALE = "stale"


class Verdict(StringEnum):
    PASS = "pass"
    ACCEPT = "accept"
    FAIL = "fail"
    REJECT = "reject"
    BLOCKED = "blocked"
    COMMENT = "comment"
    RESUME = "resume"
    ADVANCE = "advance"
    DONE = "done"
    BUDGET_HIT = "budget_hit"


RunnerExecutionStatus = RunnerStatus
PipelineState = PipelineStatus


__all__ = [
    "EngineLimitKind",
    "EngineMonitoringSource",
    "FEEDBACK_CAP",
    "LiveEventKind",
    "LiveEventRole",
    "LifecyclePhase",
    "OutcomeKind",
    "OutcomeReasonCode",
    "PipelineMode",
    "PipelineState",
    "PipelineStatus",
    "RunnerStatus",
    "RunnerExecutionStatus",
    "SubagentStatus",
    "TaskStage",
    "TaskStatus",
    "TRUNCATION_MARKER",
    "Verdict",
    "cap_feedback",
    "utcnow",
]
