"""The pipeline rule table. Nothing else lives here.

Read top to bottom to understand routing. Each row is:
    Rule(from_state=STATE, on_event=EVENT, transition_to=→ TARGET, guard?, effect?,)

Ctrl+click any S.STAGE to see the node that runs there.
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
from .stages import Stages as S
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
            transition_to=S.FAILED,
            when=exhausted_when,
            with_effect=exhaust_recovery_budget,
        ),
        Rule(
            from_state=from_state,
            on_event=on_event,
            transition_to=S.RECOVERING,
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
            transition_to=S.FAILED,
            when=when,
            with_effect=Fail(reason),
        )
    ]


RULES: list[Rule] = [
    # ── entry ─────────────────────────────────────────────
    Rule(
        from_state=S.READY,
        on_event=CleanState,
        transition_to=S.WORKTREE_SYNC,
    ),
    Rule(
        from_state=S.READY,
        on_event=NeedsPreExecRecovery,
        transition_to=S.PRE_EXEC_RECOVERY,
        with_effect=enter_pre_exec_recovery,
    ),
    # ── worktree sync ─────────────────────────────────────────────
    Rule(
        from_state=S.WORKTREE_SYNC,
        on_event=Pass,
        transition_to=entry_from_worktree_sync,
    ),
    *_terminal_reject_rules(S.WORKTREE_SYNC),
    *_recovery_rules(S.WORKTREE_SYNC, Crash),
    *_recovery_rules(S.WORKTREE_SYNC, Timeout),
    # ── pre-exec recovery ─────────────────────────────────────────────
    Rule(
        from_state=S.PRE_EXEC_RECOVERY,
        on_event=PreExecRecoverySucceeded,
        transition_to=resume_from_pre_exec,
    ),
    Rule(
        from_state=S.PRE_EXEC_RECOVERY,
        on_event=PreExecRecoveryFailed,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.PRE_EXEC_RECOVERY_FAILED),
    ),
    Rule(
        from_state=S.PRE_EXEC_RECOVERY,
        on_event=PreExecRecoveryBudgetHit,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.PRE_EXEC_RECOVERY_FAILED),
    ),
    Rule(
        from_state=S.PRE_EXEC_RECOVERY,
        on_event=Crash,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_CRASHED),
    ),
    Rule(
        from_state=S.PRE_EXEC_RECOVERY,
        on_event=Timeout,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_CRASHED),
    ),
    # ── grooming ─────────────────────────────────────────────
    Rule(
        from_state=S.BEFORE_GROOMING,
        on_event=HookOk,
        transition_to=S.GROOMING,
    ),
    Rule(
        from_state=S.GROOMING,
        on_event=Pass,
        transition_to=S.AFTER_GROOMING,
        with_effect=clear_completed_rejection_loop,
    ),
    Rule(
        from_state=S.AFTER_GROOMING,
        on_event=HookOk,
        transition_to=S.BEFORE_IMPLEMENTING,
    ),
    # ── implementing ─────────────────────────────────────────────
    Rule(
        from_state=S.BEFORE_IMPLEMENTING,
        on_event=HookOk,
        transition_to=S.IMPLEMENTING,
    ),
    Rule(
        from_state=S.IMPLEMENTING,
        on_event=Pass,
        transition_to=S.AFTER_IMPLEMENTING,
        with_effect=clear_completed_rejection_loop,
    ),
    Rule(
        from_state=S.AFTER_IMPLEMENTING,
        on_event=HookOk,
        transition_to=S.DONE,
        when=mode("single") & zero_change_shortcut(),
    ),
    Rule(
        from_state=S.AFTER_IMPLEMENTING,
        on_event=HookOk,
        transition_to=S.COMMIT,
        when=mode("single"),
    ),
    Rule(
        from_state=S.AFTER_IMPLEMENTING,
        on_event=HookOk,
        transition_to=S.BEFORE_TESTING,
        when=mode("full"),
    ),
    # ── testing ─────────────────────────────────────────────
    Rule(
        from_state=S.BEFORE_TESTING,
        on_event=HookOk,
        transition_to=S.TESTING,
    ),
    Rule(
        from_state=S.TESTING,
        on_event=Pass,
        transition_to=S.AFTER_TESTING,
        with_effect=clear_completed_rejection_loop,
    ),
    Rule(
        from_state=S.AFTER_TESTING,
        on_event=HookOk,
        transition_to=S.BEFORE_ACCEPTING,
    ),
    # ── accepting ─────────────────────────────────────────────
    Rule(
        from_state=S.BEFORE_ACCEPTING,
        on_event=HookOk,
        transition_to=S.ACCEPTING,
    ),
    Rule(
        from_state=S.ACCEPTING,
        on_event=Pass,
        transition_to=S.AFTER_ACCEPTING,
        with_effect=clear_completed_rejection_loop,
    ),
    Rule(
        from_state=S.AFTER_ACCEPTING,
        on_event=HookOk,
        transition_to=S.COMMIT,
    ),
    # ── commit ─────────────────────────────────────────────
    Rule(
        from_state=S.COMMIT,
        on_event=Pass,
        transition_to=S.AFTER_COMMIT,
    ),
    Rule(
        from_state=S.AFTER_COMMIT,
        on_event=HookOk,
        transition_to=S.DONE,
    ),
    # ── merge conflict → merge agent ─────────────────────────────────────────────
    Rule(
        from_state=S.COMMIT,
        on_event=MergeConflictDetected,
        transition_to=S.MERGE_RESOLVING,
        with_effect=stash_conflict_files,
    ),
    Rule(
        from_state=S.MERGE_RESOLVING,
        on_event=Pass,
        transition_to=S.AFTER_COMMIT,
    ),
    *_terminal_reject_rules(S.MERGE_RESOLVING),
    *_recovery_rules(S.MERGE_RESOLVING, Blocked),
    *_recovery_rules(S.MERGE_RESOLVING, Crash),
    *_recovery_rules(S.MERGE_RESOLVING, Timeout),
    # ── rejections: grooming (no retry) ─────────────────────────────────────────────
    *[rule for p in S.GROOMING_EPOCH for rule in _terminal_reject_rules(p)],
    # ── rejections: implementing / testing / accepting (retry then fail or override) ─────────────────────────────────────────────
    *retry_epoch_rules(
        S.IMPLEMENTING,
        S.IMPLEMENTING_EPOCH,
        retry_target=S.IMPLEMENTING,
        exhausted_reason=FailedReason.SEMANTIC_REJECT,
    ),
    Rule(
        from_state=S.TESTING,
        on_event=Reject,
        transition_to=S.ACCEPTING,
        when=stage_retries_exhausted(PipelineState.TESTING) & last_hook_ok(),
        with_effect=RememberRejection(PipelineState.ACCEPTING),
    ),
    *retry_epoch_rules(
        S.TESTING,
        S.TESTING_EPOCH,
        retry_target=S.IMPLEMENTING,
        exhausted_reason=FailedReason.SEMANTIC_REJECT,
    ),
    *retry_epoch_rules(
        S.ACCEPTING,
        S.ACCEPTING_EPOCH,
        retry_target=S.IMPLEMENTING,
        exhausted_reason=FailedReason.SEMANTIC_REJECT,
    ),
    # ── rejections: commit (no retry) ─────────────────────────────────────────────
    *[rule for p in S.COMMIT_EPOCH for rule in _terminal_reject_rules(p)],
    # ── blocked ─────────────────────────────────────────────
    *_recovery_rules(S.GROOMING, Blocked),
    *_recovery_rules(S.IMPLEMENTING, Blocked),
    *_recovery_rules(S.TESTING, Blocked),
    *_recovery_rules(S.ACCEPTING, Blocked),
    # ── escalations ─────────────────────────────────────────────
    Rule(
        from_state=S.ALL_STAGE_PHASES,
        on_event=StageRetryLimitHit,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.UNRECOVERABLE_ERROR),
    ),
    Rule(
        from_state=S.ALL_STAGE_PHASES,
        on_event=OverallRetryLimitHit,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.UNRECOVERABLE_ERROR),
    ),
    Rule(
        from_state=S.ALL_STAGE_PHASES,
        on_event=TaskTimeBudgetExceeded,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.TIME_BUDGET_EXCEEDED),
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=TaskTimeBudgetExceeded,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.TIME_BUDGET_EXCEEDED),
    ),
    # ── recovering ─────────────────────────────────────────────
    Rule(
        from_state=S.RECOVERING,
        on_event=RecoverySucceeded,
        transition_to=resume_from_origin,
        when=recovery_resume_is_concrete(),
        with_effect=record_recovery_success,
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=RecoverySucceeded,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_MISSING_TARGET_STAGE),
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=RecoveryFailed,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_EXHAUSTED),
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=RecoveryBudgetHit,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_BUDGET_HIT),
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=Crash,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_CRASHED),
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=Timeout,
        transition_to=S.FAILED,
        with_effect=Fail(FailedReason.RECOVERY_CRASHED),
    ),
    # ── wildcards (must be last) ─────────────────────────────────────────────
    *_recovery_rules(S.ALL_STAGE_PHASES, Crash),
    *_recovery_rules(S.ALL_STAGE_PHASES, Timeout),
]
