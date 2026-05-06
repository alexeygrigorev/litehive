"""The pipeline rule table. Nothing else lives here.

Read top to bottom to understand routing. Each row is:
    Rule(from_state=STATE, on_event=EVENT, transition_to=→ TARGET, guard?, effect?,)

Ctrl+click any Stages.STAGE to see the node that runs there.
"""

from litehive.domain.common import PipelineState
from litehive.domain.lifecycle_deltas import (
    clear_completed_rejection_loop,
    enter_pre_exec_recovery,
    enter_recovery,
    exhaust_recovery_budget,
    Fail,
    RememberRejection,
    record_recovery_success,
    stash_conflict_files,
)
from .types import PipelineMode
from .events import (
    Blocked,
    CleanState,
    Crash,
    HookOk,
    MergeConflictDetected,
    NeedsPreExecRecovery,
    OverallRetryLimitHit,
    Pass,
    PreExecRecoveryBudgetHit,
    PreExecRecoveryFailed,
    PreExecRecoverySucceeded,
    RecoveryBudgetHit,
    RecoveryFailed,
    RecoverySucceeded,
    Reject,
    StageRetryLimitHit,
    TaskTimeBudgetExceeded,
    Timeout,
)
from .guards import (
    last_hook_ok,
    mode,
    recovery_budget_available,
    recovery_budget_exhausted,
    recovery_resume_is_concrete,
    stage_retries_exhausted,
    zero_change_shortcut,
)
from .stages import Stages
from .transitions import Rule, entry_from_worktree_sync, resume_from_origin, resume_from_pre_exec, retry_epoch_rules
from .types import FailedReason


def _recovery_rules(from_state, on_event, when=None) -> list[Rule]:
    """Expand one (state, event) pair into the two-rule budget-aware recovery routing the rule table needs everywhere; one rule fails when the recovery budget is exhausted, the other enters RECOVERING when budget remains."""
    if when is None:
        exhausted_when = recovery_budget_exhausted()
        available_when = recovery_budget_available()
    else:
        exhausted_when = when & recovery_budget_exhausted()
        available_when = when & recovery_budget_available()
    return [
        Rule(
            from_state=from_state,
            on_event=on_event,
            transition_to=Stages.FAILED,
            when=exhausted_when,
            with_effect=exhaust_recovery_budget,
        ),
        Rule(
            from_state=from_state,
            on_event=on_event,
            transition_to=Stages.RECOVERING,
            when=available_when,
            with_effect=enter_recovery,
        ),
    ]


def _terminal_reject_rules(from_state, when=None, reason: FailedReason = FailedReason.SEMANTIC_REJECT) -> list[Rule]:
    """Build the "Reject from this state goes straight to FAILED" rule for stages where rejection is non-retryable (grooming, commit, merge-resolving)."""
    return [
        Rule(
            from_state=from_state,
            on_event=Reject,
            transition_to=Stages.FAILED,
            when=when,
            with_effect=Fail(reason),
        )
    ]


def _epoch_terminal_reject_rules(epoch) -> list[Rule]:
    """
    Expand ``_terminal_reject_rules`` over every stage in an epoch.

    Used by the grooming and commit blocks of ``RULES``: each
    epoch is a tuple of pipeline states, and every state in the
    tuple should reject straight to ``FAILED``. Flattening here
    keeps the spread sites in ``RULES`` readable.
    """
    rules: list[Rule] = []
    for stage in epoch:
        rules.extend(_terminal_reject_rules(stage))
    return rules


RULES: list[Rule] = [
    # ── entry ─────────────────────────────────────────────
    Rule(
        from_state=Stages.READY,
        on_event=CleanState,
        transition_to=Stages.WORKTREE_SYNC,
    ),
    Rule(
        from_state=Stages.READY,
        on_event=NeedsPreExecRecovery,
        transition_to=Stages.PRE_EXEC_RECOVERY,
        with_effect=enter_pre_exec_recovery,
    ),
    # ── worktree sync ─────────────────────────────────────────────
    Rule(
        from_state=Stages.WORKTREE_SYNC,
        on_event=Pass,
        transition_to=entry_from_worktree_sync,
    ),
    *_terminal_reject_rules(Stages.WORKTREE_SYNC),
    *_recovery_rules(Stages.WORKTREE_SYNC, Crash),
    *_recovery_rules(Stages.WORKTREE_SYNC, Timeout),
    # ── pre-exec recovery ─────────────────────────────────────────────
    Rule(
        from_state=Stages.PRE_EXEC_RECOVERY,
        on_event=PreExecRecoverySucceeded,
        transition_to=resume_from_pre_exec,
    ),
    Rule(
        from_state=Stages.PRE_EXEC_RECOVERY,
        on_event=PreExecRecoveryFailed,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.PRE_EXEC_RECOVERY_FAILED),
    ),
    Rule(
        from_state=Stages.PRE_EXEC_RECOVERY,
        on_event=PreExecRecoveryBudgetHit,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.PRE_EXEC_RECOVERY_FAILED),
    ),
    Rule(
        from_state=Stages.PRE_EXEC_RECOVERY,
        on_event=Crash,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_CRASHED),
    ),
    Rule(
        from_state=Stages.PRE_EXEC_RECOVERY,
        on_event=Timeout,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_CRASHED),
    ),
    # ── grooming ─────────────────────────────────────────────
    Rule(
        from_state=Stages.BEFORE_GROOMING,
        on_event=HookOk,
        transition_to=Stages.GROOMING,
    ),
    Rule(
        from_state=Stages.GROOMING,
        on_event=Pass,
        transition_to=Stages.AFTER_GROOMING,
        with_effect=clear_completed_rejection_loop,
    ),
    Rule(
        from_state=Stages.AFTER_GROOMING,
        on_event=HookOk,
        transition_to=Stages.BEFORE_IMPLEMENTING,
    ),
    # ── implementing ─────────────────────────────────────────────
    Rule(
        from_state=Stages.BEFORE_IMPLEMENTING,
        on_event=HookOk,
        transition_to=Stages.IMPLEMENTING,
    ),
    Rule(
        from_state=Stages.IMPLEMENTING,
        on_event=Pass,
        transition_to=Stages.AFTER_IMPLEMENTING,
        with_effect=clear_completed_rejection_loop,
    ),
    Rule(
        from_state=Stages.AFTER_IMPLEMENTING,
        on_event=HookOk,
        transition_to=Stages.DONE,
        when=mode(PipelineMode.SINGLE) & zero_change_shortcut(),
    ),
    Rule(
        from_state=Stages.AFTER_IMPLEMENTING,
        on_event=HookOk,
        transition_to=Stages.COMMIT,
        when=mode(PipelineMode.SINGLE),
    ),
    Rule(
        from_state=Stages.AFTER_IMPLEMENTING,
        on_event=HookOk,
        transition_to=Stages.BEFORE_TESTING,
        when=mode(PipelineMode.FULL),
    ),
    # ── testing ─────────────────────────────────────────────
    Rule(
        from_state=Stages.BEFORE_TESTING,
        on_event=HookOk,
        transition_to=Stages.TESTING,
    ),
    Rule(
        from_state=Stages.TESTING,
        on_event=Pass,
        transition_to=Stages.AFTER_TESTING,
        with_effect=clear_completed_rejection_loop,
    ),
    Rule(
        from_state=Stages.AFTER_TESTING,
        on_event=HookOk,
        transition_to=Stages.BEFORE_ACCEPTING,
    ),
    # ── accepting ─────────────────────────────────────────────
    Rule(
        from_state=Stages.BEFORE_ACCEPTING,
        on_event=HookOk,
        transition_to=Stages.ACCEPTING,
    ),
    Rule(
        from_state=Stages.ACCEPTING,
        on_event=Pass,
        transition_to=Stages.AFTER_ACCEPTING,
        with_effect=clear_completed_rejection_loop,
    ),
    Rule(
        from_state=Stages.AFTER_ACCEPTING,
        on_event=HookOk,
        transition_to=Stages.COMMIT,
    ),
    # ── commit ─────────────────────────────────────────────
    Rule(
        from_state=Stages.COMMIT,
        on_event=Pass,
        transition_to=Stages.AFTER_COMMIT,
    ),
    Rule(
        from_state=Stages.AFTER_COMMIT,
        on_event=HookOk,
        transition_to=Stages.DONE,
    ),
    # ── merge conflict → merge agent ─────────────────────────────────────────────
    Rule(
        from_state=Stages.COMMIT,
        on_event=MergeConflictDetected,
        transition_to=Stages.MERGE_RESOLVING,
        with_effect=stash_conflict_files,
    ),
    Rule(
        from_state=Stages.MERGE_RESOLVING,
        on_event=Pass,
        transition_to=Stages.AFTER_COMMIT,
    ),
    *_terminal_reject_rules(Stages.MERGE_RESOLVING),
    *_recovery_rules(Stages.MERGE_RESOLVING, Blocked),
    *_recovery_rules(Stages.MERGE_RESOLVING, Crash),
    *_recovery_rules(Stages.MERGE_RESOLVING, Timeout),
    # ── rejections: grooming (no retry) ─────────────────────────────────────────────
    *_epoch_terminal_reject_rules(Stages.GROOMING_EPOCH),
    # ── rejections: implementing / testing / accepting (retry then fail or override) ─────────────────────────────────────────────
    *retry_epoch_rules(
        Stages.IMPLEMENTING,
        Stages.IMPLEMENTING_EPOCH,
        retry_target=Stages.IMPLEMENTING,
        exhausted_reason=FailedReason.SEMANTIC_REJECT,
    ),
    Rule(
        from_state=Stages.TESTING,
        on_event=Reject,
        transition_to=Stages.ACCEPTING,
        when=stage_retries_exhausted(PipelineState.TESTING) & last_hook_ok(),
        with_effect=RememberRejection(PipelineState.ACCEPTING),
    ),
    *retry_epoch_rules(
        Stages.TESTING,
        Stages.TESTING_EPOCH,
        retry_target=Stages.IMPLEMENTING,
        exhausted_reason=FailedReason.SEMANTIC_REJECT,
    ),
    *retry_epoch_rules(
        Stages.ACCEPTING,
        Stages.ACCEPTING_EPOCH,
        retry_target=Stages.IMPLEMENTING,
        exhausted_reason=FailedReason.SEMANTIC_REJECT,
    ),
    # ── rejections: commit (no retry) ─────────────────────────────────────────────
    *_epoch_terminal_reject_rules(Stages.COMMIT_EPOCH),
    # ── blocked ─────────────────────────────────────────────
    *_recovery_rules(Stages.GROOMING, Blocked),
    *_recovery_rules(Stages.IMPLEMENTING, Blocked),
    *_recovery_rules(Stages.TESTING, Blocked),
    *_recovery_rules(Stages.ACCEPTING, Blocked),
    # ── escalations ─────────────────────────────────────────────
    Rule(
        from_state=Stages.ALL_STAGE_PHASES,
        on_event=StageRetryLimitHit,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.UNRECOVERABLE_ERROR),
    ),
    Rule(
        from_state=Stages.ALL_STAGE_PHASES,
        on_event=OverallRetryLimitHit,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.UNRECOVERABLE_ERROR),
    ),
    Rule(
        from_state=Stages.ALL_STAGE_PHASES,
        on_event=TaskTimeBudgetExceeded,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.TIME_BUDGET_EXCEEDED),
    ),
    Rule(
        from_state=Stages.RECOVERING,
        on_event=TaskTimeBudgetExceeded,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.TIME_BUDGET_EXCEEDED),
    ),
    # ── recovering ─────────────────────────────────────────────
    Rule(
        from_state=Stages.RECOVERING,
        on_event=RecoverySucceeded,
        transition_to=resume_from_origin,
        when=recovery_resume_is_concrete(),
        with_effect=record_recovery_success,
    ),
    Rule(
        from_state=Stages.RECOVERING,
        on_event=RecoverySucceeded,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_MISSING_TARGET_STAGE),
    ),
    Rule(
        from_state=Stages.RECOVERING,
        on_event=RecoveryFailed,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_EXHAUSTED),
    ),
    Rule(
        from_state=Stages.RECOVERING,
        on_event=RecoveryBudgetHit,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_BUDGET_HIT),
    ),
    Rule(
        from_state=Stages.RECOVERING,
        on_event=Crash,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_CRASHED),
    ),
    Rule(
        from_state=Stages.RECOVERING,
        on_event=Timeout,
        transition_to=Stages.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_CRASHED),
    ),
    # ── wildcards (must be last) ─────────────────────────────────────────────
    *_recovery_rules(Stages.ALL_STAGE_PHASES, Crash),
    *_recovery_rules(Stages.ALL_STAGE_PHASES, Timeout),
]
