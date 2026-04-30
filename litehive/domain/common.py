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
TRUNCATION_MARKER = "\n\n… [truncated — full execution trace in subagent artifacts]"


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
    """Terminal outcome categories for tasks.

    Used by TaskService and CLI close actions to classify why a task ended.
    Maps to task close reasons and interruption types for reporting and
    operator workflow management.
    """

    DONE = "done"  # Task was already or successfully completed
    CLOSED = "closed"  # Explicitly closed with a close_reason
    FLAGGED = "flagged"  # Requires explicit operator attention
    BLOCKED = "blocked"  # Progress requires external input or missing dependency
    INTERRUPTED = "interrupted"  # Execution stopped, potentially resumable
    CANCELLED = "cancelled"  # Operator intentionally stopped this task
    WONT_DO = "wont_do"  # Task is no longer worth doing
    DEFERRED = "deferred"  # Task should wait for later
    DUPLICATE = "duplicate"  # Another task already covers the same work


class OutcomeReasonCode(StringEnum):
    """Normalized reason codes for stage outcomes and task interruptions.

    Captures the specific machine-readable reason behind a stage verdict or
    task interruption. Differs from Verdict by providing more specific context:
    - Verdict answers "was this accepted?" (PASS/REJECT/BLOCKED)
    - OutcomeReasonCode answers "what specifically caused that outcome?"

    Used by PipelineRunner for routing decisions, recovery logic, and
    reporting for machine-readable summaries and filtering.
    """

    VERDICT_FAIL = "verdict_fail"
    VERDICT_REJECT = "verdict_reject"
    VERDICT_BLOCKED = "verdict_blocked"
    BLOCKED_ON_FOLLOW_UP = "blocked_on_follow_up"
    HALLUCINATED_COMPLETION = "hallucinated_completion"
    MISSING_ACCEPTANCE_CRITERIA = "missing_acceptance_criteria"
    RETRY_LIMIT_EXHAUSTED = "retry_limit_exhausted"
    STAGE_RETRY_LIMIT_EXHAUSTED = "stage_retry_limit_exhausted"
    EXECUTION_INTERRUPTED = "execution_interrupted"
    EXECUTION_CANCELLED = "execution_cancelled"
    STAGE_EXCEPTION = "stage_exception"
    UNSUPPORTED_VERDICT = "unsupported_verdict"
    MERGE_CONFLICT = "merge_conflict"
    DONE = "done"
    WONT_DO = "wont_do"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"


class PipelineMode(StringEnum):
    """Top-level execution mode for a task.

    Set by task creation or operator task-edit commands. Used by PipelineRunner
    when deciding which states are eligible for the task.
    """

    SINGLE = "single"  # Skip early planning states, start directly in implementation
    FULL = "full"  # Run the full pipeline from grooming through commit


class PipelineState(StringEnum):
    """Canonical internal state-machine positions.

    These are the real nodes the pipeline runner persists, evaluates in
    transition rules, and passes into prompts. They are intentionally separate
    from ``PipelineStatus`` and ``TaskStage``, which are operator-facing
    projections that collapse hook/system nodes into broader task phases.
    """

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

    COMMIT = "commit"
    AFTER_COMMIT = "after_commit"
    MERGE_RESOLVING = "merge_resolving"

    RECOVERING = "recovering"
    DONE = "done"
    FAILED = "failed"


class TaskStage(StringEnum):
    """Main execution stages in the task lifecycle.

    Represents the high-level work phases that a task progresses through.
    Used for coarse-grained tracking and reporting; detailed runner phases
    are represented by node names in litehive.lifecycle.types.
    """

    GROOMING = "grooming"  # Initial planning and requirement analysis
    IMPLEMENTING = "implementing"  # Core development work
    TESTING = "testing"  # Validation and testing phase
    ACCEPTING = "accepting"  # Final review and acceptance
    COMMIT_TO_GIT = "commit_to_git"  # Git commit and merge operations


class TaskStatus(StringEnum):
    """High-level execution or terminal category for a task.

    Set by TaskService, PipelineRunner, and operator-facing CLI commands.
    Used by queueing, filtering, reporting, and operator decisions about
    what happens next.

    Note: There is no separate 'cancelled' status because 'close_reason'
    explains why a task was closed, avoiding status/reason duplication.
    """

    QUEUED = "queued"  # Waiting in the queue
    IN_PROGRESS = "in_progress"  # Currently executing
    INTERRUPTED = "interrupted"  # Execution stopped, potentially resumable
    PARKED = "parked"  # Intentionally paused by Litehive or operator
    DONE = "done"  # Completed successfully
    CLOSED = "closed"  # Explicitly closed with a close_reason
    ARCHIVED = "archived"  # Completed and moved to history-only archive
    FLAGGED = "flagged"  # Requires explicit operator attention


class PipelineStatus(StringEnum):
    """Operator-facing projection of internal pipeline progress.

    This is not the pipeline state machine. It collapses detailed
    ``PipelineState`` nodes, including hook and system nodes, into the coarse
    progress buckets shown in CLI/operator views and persisted on task runtime
    records for filtering and display.
    """

    BACKLOG = "backlog"  # Not yet started
    GROOMING = "grooming"  # In planning phase
    IMPLEMENTING = "implementing"  # In development phase
    TESTING = "testing"  # In validation phase
    ACCEPTING = "accepting"  # In review phase
    COMMIT_TO_GIT = "commit_to_git"  # In git operations phase
    DONE = "done"  # Completed successfully
    FLAGGED = "flagged"  # Requires operator attention


def canonical_pipeline_state(value: str | PipelineState) -> PipelineState:
    """Normalize a persisted or caller-supplied value to ``PipelineState``."""
    if isinstance(value, PipelineState):
        return value
    return PipelineState(str(value))


_TASK_STAGE_BY_PIPELINE_STATE: dict[PipelineState, TaskStage] = {
    PipelineState.BEFORE_GROOMING: TaskStage.GROOMING,
    PipelineState.GROOMING: TaskStage.GROOMING,
    PipelineState.AFTER_GROOMING: TaskStage.GROOMING,
    PipelineState.RECOVERING: TaskStage.GROOMING,
    PipelineState.BEFORE_IMPLEMENTING: TaskStage.IMPLEMENTING,
    PipelineState.IMPLEMENTING: TaskStage.IMPLEMENTING,
    PipelineState.AFTER_IMPLEMENTING: TaskStage.IMPLEMENTING,
    PipelineState.BEFORE_TESTING: TaskStage.TESTING,
    PipelineState.TESTING: TaskStage.TESTING,
    PipelineState.AFTER_TESTING: TaskStage.TESTING,
    PipelineState.BEFORE_ACCEPTING: TaskStage.ACCEPTING,
    PipelineState.ACCEPTING: TaskStage.ACCEPTING,
    PipelineState.AFTER_ACCEPTING: TaskStage.ACCEPTING,
    PipelineState.COMMIT: TaskStage.COMMIT_TO_GIT,
    PipelineState.AFTER_COMMIT: TaskStage.COMMIT_TO_GIT,
    PipelineState.MERGE_RESOLVING: TaskStage.COMMIT_TO_GIT,
}


_PIPELINE_STATUS_BY_PIPELINE_STATE: dict[PipelineState, PipelineStatus] = {
    PipelineState.READY: PipelineStatus.BACKLOG,
    PipelineState.WORKTREE_SYNC: PipelineStatus.BACKLOG,
    PipelineState.RECOVERING_PRE_EXEC: PipelineStatus.BACKLOG,
    PipelineState.BEFORE_GROOMING: PipelineStatus.GROOMING,
    PipelineState.GROOMING: PipelineStatus.GROOMING,
    PipelineState.AFTER_GROOMING: PipelineStatus.GROOMING,
    PipelineState.RECOVERING: PipelineStatus.GROOMING,
    PipelineState.BEFORE_IMPLEMENTING: PipelineStatus.IMPLEMENTING,
    PipelineState.IMPLEMENTING: PipelineStatus.IMPLEMENTING,
    PipelineState.AFTER_IMPLEMENTING: PipelineStatus.IMPLEMENTING,
    PipelineState.BEFORE_TESTING: PipelineStatus.TESTING,
    PipelineState.TESTING: PipelineStatus.TESTING,
    PipelineState.AFTER_TESTING: PipelineStatus.TESTING,
    PipelineState.BEFORE_ACCEPTING: PipelineStatus.ACCEPTING,
    PipelineState.ACCEPTING: PipelineStatus.ACCEPTING,
    PipelineState.AFTER_ACCEPTING: PipelineStatus.ACCEPTING,
    PipelineState.COMMIT: PipelineStatus.COMMIT_TO_GIT,
    PipelineState.AFTER_COMMIT: PipelineStatus.COMMIT_TO_GIT,
    PipelineState.MERGE_RESOLVING: PipelineStatus.COMMIT_TO_GIT,
    PipelineState.DONE: PipelineStatus.DONE,
    PipelineState.FAILED: PipelineStatus.FLAGGED,
}


def task_stage_for_pipeline_state(value: str | PipelineState) -> TaskStage | None:
    """Return the user-facing work stage for an internal pipeline state."""
    return _TASK_STAGE_BY_PIPELINE_STATE.get(canonical_pipeline_state(value))


def pipeline_status_for_pipeline_state(value: str | PipelineState) -> PipelineStatus:
    """Return the operator-facing ``PipelineStatus`` projection for a machine state."""
    return _PIPELINE_STATUS_BY_PIPELINE_STATE[canonical_pipeline_state(value)]


class RunnerStatus(StringEnum):
    """Status for monitoring the top-level runner process.

    Used by monitoring and operator interfaces to track the health and
    activity state of the task execution runner.
    """

    IDLE = "idle"  # Runner is active but not executing a task
    RUNNING = "running"  # Runner is actively executing a task
    LATE = "late"  # Runner missed expected heartbeat timing
    STALE = "stale"  # Runner appears to be unresponsive


class Verdict(StringEnum):
    """Decision submitted for an executable pipeline state.

    Created by subagents and hook execution paths when they submit the result
    of a pipeline state. Used by PipelineRunner to decide whether to advance,
    retry, block, or enter recovery. Also used by ActivityEntry and
    TaskOutcome as the canonical submitted decision value. ``StageReport`` maps
    submitted decisions into its narrower canonical ``pass/reject/blocked``
    verdict set.
    """

    PASS = "pass"  # General positive outcome
    ACCEPT = "accept"  # Stage goal was achieved
    FAIL = "fail"  # General negative outcome
    REJECT = "reject"  # Result not acceptable, but can continue
    BLOCKED = "blocked"  # Progress requires external operator input
    COMMENT = "comment"  # Informational, no decision
    RESUME = "resume"  # Continue from where left off
    ADVANCE = "advance"  # Move to next stage
    DONE = "done"  # Task completed successfully
    BUDGET_HIT = "budget_hit"  # Resource limits reached


RunnerExecutionStatus = RunnerStatus


__all__ = [
    "EngineLimitKind",
    "EngineMonitoringSource",
    "FEEDBACK_CAP",
    "LiveEventKind",
    "LiveEventRole",
    "OutcomeKind",
    "OutcomeReasonCode",
    "PipelineState",
    "PipelineMode",
    "PipelineStatus",
    "RunnerStatus",
    "RunnerExecutionStatus",
    "SubagentStatus",
    "TaskStage",
    "TaskStatus",
    "TRUNCATION_MARKER",
    "Verdict",
    "cap_feedback",
    "canonical_pipeline_state",
    "pipeline_status_for_pipeline_state",
    "task_stage_for_pipeline_state",
    "utcnow",
]
