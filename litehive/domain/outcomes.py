"""
Task outcome vocabulary.

`TaskOutcomeKind` names the terminal bucket a task landed in:
done, flagged, blocked, interrupted, cancelled, duplicate, and so on.
Queue filtering, status summaries, and recovery gates use this broad
bucket to decide whether a task is still runnable.

`OutcomeReasonCode` names the machine-readable reason for that bucket.
It is the more specific routing fact: timeout, missing acceptance
criteria, operator cancellation, retry exhaustion, or a close reason.
The same broad kind can have different reason codes, and those reason
codes drive different recovery and reporting paths.

`Verdict` is intentionally separate. Agent and hook reports use verdicts
to say whether a stage report passed, rejected, blocked, or only left a
comment. A verdict can lead to a task outcome, but it is not itself the
task outcome.
"""

from litehive.domain.common import StringEnum


class TaskOutcomeKind(StringEnum):
    """
    Terminal outcome categories for a finished task.

    Tells the operator and downstream filters why a task is no longer
    running: completion vs. operator close vs. blocked vs. cancelled vs.
    duplicate. Set by task runtime and close flows; consumed by
    reporting, queue filtering, and recovery decisions.
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


class TaskCloseReason(StringEnum):
    """
    Operator-facing reasons accepted by `litehive task close`.

    These values are persisted on `TaskRecord.close_reason` and shown
    back to operators. They are intentionally separate from
    `OutcomeReasonCode`, which records the broader machine-readable
    runtime cause on `TaskOutcomeState`.
    """

    DONE = "done"
    WONT_DO = "wont_do"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"

    @property
    def outcome_reason_code(self) -> "OutcomeReasonCode":
        """
        Return the runtime reason-code bucket for this close reason.
        """
        if self == TaskCloseReason.DONE:
            return OutcomeReasonCode.TASK_DONE
        return OutcomeReasonCode.TASK_CLOSED

    @property
    def task_close_label(self) -> str:
        """
        Default human-readable journal reason for operator closures.
        """
        match self:
            case TaskCloseReason.DONE:
                return "Task already satisfied."
            case TaskCloseReason.WONT_DO:
                return "Task closed as won't do."
            case TaskCloseReason.DEFERRED:
                return "Task deferred."
            case TaskCloseReason.DUPLICATE:
                return "Task closed as duplicate."


class OutcomeReasonCode(StringEnum):
    """
    Normalized reason codes for stage outcomes and task interruptions.

    `OutcomeReasonCode` answers what specifically caused an outcome
    so two rejections with different root causes can be distinguished
    for routing and reporting.
    """

    VERDICT_FAIL = "verdict_fail"  # Agent or hook submitted a generic fail verdict
    VERDICT_REJECT = "verdict_reject"  # QA/reviewer judged the stage result unacceptable
    VERDICT_BLOCKED = "verdict_blocked"  # Stage cannot proceed without external input
    HALLUCINATED_COMPLETION = "hallucinated_completion"  # Agent claimed done without satisfying acceptance criteria
    MISSING_ACCEPTANCE_CRITERIA = "missing_acceptance_criteria"  # Task has no measurable completion conditions
    RETRY_LIMIT_EXHAUSTED = "retry_limit_exhausted"  # Overall task retry budget fully consumed
    STAGE_RETRY_LIMIT_EXHAUSTED = "stage_retry_limit_exhausted"  # Per-stage retry budget fully consumed
    EXECUTION_INTERRUPTED = "execution_interrupted"  # Potentially resumable stop with interruption context
    EXECUTION_CANCELLED = "execution_cancelled"  # Deliberate operator abandon/kill path
    STAGE_EXCEPTION = "stage_exception"  # Unhandled exception during stage execution
    UNSUPPORTED_VERDICT = "unsupported_verdict"  # Submitted verdict not recognized by routing logic
    MERGE_CONFLICT = "merge_conflict"  # Unresolved conflicts after a merge attempt
    TASK_DONE = "task_done"  # Task completed successfully or was already satisfied
    TASK_CLOSED = "task_closed"  # Operator explicitly closed the task with a close reason
