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
    """Terminal outcome categories for tasks that don't complete successfully.

    Used by TaskService and CLI close actions to classify why a task ended
    without successful completion. Maps to task close reasons and interruption
    types for reporting and operator workflow management.
    """
    FLAGGED = "flagged"      # Requires explicit operator attention
    BLOCKED = "blocked"      # Progress requires external input or missing dependency
    INTERRUPTED = "interrupted"  # Execution stopped, potentially resumable
    CANCELLED = "cancelled"  # Operator intentionally stopped this task
    WONT_DO = "wont_do"     # Task is no longer worth doing
    DEFERRED = "deferred"   # Task should wait for later
    DUPLICATE = "duplicate" # Another task already covers the same work


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
    WONT_DO = "wont_do"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"


class PipelineMode(StringEnum):
    """Top-level execution mode for a task.

    Set by task creation or operator task-edit commands. Used by PipelineRunner
    when deciding which states are eligible for the task.
    """
    SINGLE = "single"  # Skip early planning states, start directly in implementation
    FULL = "full"      # Run the full pipeline from grooming through commit


class TaskStage(StringEnum):
    """Main execution stages in the task lifecycle.

    Represents the high-level work phases that a task progresses through.
    Used for coarse-grained tracking and reporting; detailed runner phases
    are represented by node names in litehive.lifecycle.types.
    """
    GROOMING = "grooming"        # Initial planning and requirement analysis
    IMPLEMENTING = "implementing"  # Core development work
    TESTING = "testing"         # Validation and testing phase
    ACCEPTING = "accepting"     # Final review and acceptance
    COMMIT_TO_GIT = "commit_to_git"  # Git commit and merge operations

class TaskStatus(StringEnum):
    """High-level execution or terminal category for a task.

    Set by TaskService, PipelineRunner, and operator-facing CLI commands.
    Used by queueing, filtering, reporting, and operator decisions about
    what happens next.

    Note: There is no separate 'cancelled' status because 'close_reason'
    explains why a task was closed, avoiding status/reason duplication.
    """
    QUEUED = "queued"               # Waiting in the queue
    IN_PROGRESS = "in_progress"     # Currently executing
    INTERRUPTED = "interrupted"     # Execution stopped, potentially resumable
    PARKED = "parked"              # Intentionally paused by Litehive or operator
    DONE = "done"                  # Completed successfully
    ARCHIVED = "archived"          # Completed and moved to history-only archive
    FLAGGED = "flagged"            # Requires explicit operator attention
    MERGE_FAILED = "merge_failed"   # Failed during git merge operation
    CANCELLED = "cancelled"         # Operator intentionally stopped this task
    WONT_DO = "wont_do"            # Task is no longer worth doing
    DEFERRED = "deferred"          # Task should wait for later
    DUPLICATE = "duplicate"         # Another task already covers the same work


class PipelineStatus(StringEnum):
    """Simplified pipeline state view for external reporting and displays.

    Provides a coarser-grained view for operator interfaces and reporting.
    Used by CLI views and task persistence.
    """
    BACKLOG = "backlog"               # Not yet started
    GROOMING = "grooming"             # In planning phase
    IMPLEMENTING = "implementing"     # In development phase
    TESTING = "testing"              # In validation phase
    ACCEPTING = "accepting"          # In review phase
    COMMIT_TO_GIT = "commit_to_git"  # In git operations phase
    DONE = "done"                    # Completed successfully
    MERGE_FAILED = "merge_failed"    # Failed during git merge
    FLAGGED = "flagged"              # Requires operator attention


class RunnerStatus(StringEnum):
    """Status for monitoring the top-level runner process.

    Used by monitoring and operator interfaces to track the health and
    activity state of the task execution runner.
    """
    IDLE = "idle"        # Runner is active but not executing a task
    RUNNING = "running"  # Runner is actively executing a task
    LATE = "late"        # Runner missed expected heartbeat timing
    STALE = "stale"      # Runner appears to be unresponsive


class Verdict(StringEnum):
    """Decision submitted for an executable pipeline state.

    Created by subagents and hook execution paths when they submit the result
    of a pipeline state. Used by PipelineRunner to decide whether to advance,
    retry, block, or enter recovery. Also used by ActivityEntry, StageReport,
    and TaskOutcome as the canonical decision value.
    """
    PASS = "pass"              # General positive outcome
    ACCEPT = "accept"          # Stage goal was achieved
    FAIL = "fail"              # General negative outcome
    REJECT = "reject"          # Result not acceptable, but can continue
    BLOCKED = "blocked"        # Progress requires external operator input
    COMMENT = "comment"        # Informational, no decision
    RESUME = "resume"          # Continue from where left off
    ADVANCE = "advance"        # Move to next stage
    DONE = "done"              # Task completed successfully
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
    "utcnow",
]
