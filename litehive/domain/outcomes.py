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


class OutcomeReasonCode(StringEnum):
    """
    Normalized reason codes for stage outcomes and task interruptions.

    `OutcomeReasonCode` answers what specifically caused an outcome
    so two rejections with different root causes can be distinguished
    for routing and reporting.
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

    @property
    def is_task_close_outcome(self) -> bool:
        """
        Whether `litehive task close` may use this reason directly.
        """
        match self:
            case (
                OutcomeReasonCode.DONE
                | OutcomeReasonCode.WONT_DO
                | OutcomeReasonCode.DEFERRED
                | OutcomeReasonCode.DUPLICATE
                | OutcomeReasonCode.EXECUTION_CANCELLED
            ):
                return True
            case _:
                return False

    @property
    def task_close_label(self) -> str | None:
        """
        Default human-readable close reason for operator closures.
        """
        match self:
            case OutcomeReasonCode.DONE:
                return "Task already satisfied."
            case OutcomeReasonCode.WONT_DO:
                return "Task closed as won't do."
            case OutcomeReasonCode.DEFERRED:
                return "Task deferred."
            case OutcomeReasonCode.DUPLICATE:
                return "Task closed as duplicate."
            case OutcomeReasonCode.EXECUTION_CANCELLED:
                return "Task abandoned via CLI."
            case _:
                return None
