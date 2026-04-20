"""The pipeline rule table. Nothing else lives here.

Read top to bottom to understand routing. Each row is:
    Rule(from_state=STATE, on_event=EVENT, transition_to=→ TARGET, guard?, effect?,)

Ctrl+click any S.STAGE to see the node that runs there.
"""

from litehive.domain.lifecycle_deltas import (
    clear_completed_rejection_loop,
    clear_recovery_attempt,
    enter_pre_exec_recovery,
    enter_recovery,
    exhaust_recovery_budget,
    fail,
    remember_rejection,
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


def _recovery_rules(from_state, on_event, *, when=None) -> list[Rule]:
    return [
        Rule(
            from_state=from_state,
            on_event=on_event,
            transition_to=S.FAILED,
            when=recovery_budget_exhausted() if when is None else when & recovery_budget_exhausted(),
            with_effect=exhaust_recovery_budget,
        ),
        Rule(
            from_state=from_state,
            on_event=on_event,
            transition_to=S.RECOVERING,
            when=recovery_budget_available() if when is None else when & recovery_budget_available(),
            with_effect=enter_recovery,
        ),
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
    *_recovery_rules(S.WORKTREE_SYNC, Reject),
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
        with_effect=fail("pre_exec_recovery_failed"),
    ),
    Rule(
        from_state=S.PRE_EXEC_RECOVERY,
        on_event=PreExecRecoveryBudgetHit,
        transition_to=S.FAILED,
        with_effect=fail("pre_exec_recovery_failed"),
    ),
    Rule(
        from_state=S.PRE_EXEC_RECOVERY,
        on_event=Crash,
        transition_to=S.FAILED,
        with_effect=fail("recovery_crashed"),
    ),
    Rule(
        from_state=S.PRE_EXEC_RECOVERY,
        on_event=Timeout,
        transition_to=S.FAILED,
        with_effect=fail("recovery_crashed"),
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
    *_recovery_rules(S.MERGE_RESOLVING, Reject),
    *_recovery_rules(S.MERGE_RESOLVING, Blocked),
    *_recovery_rules(S.MERGE_RESOLVING, Crash),
    *_recovery_rules(S.MERGE_RESOLVING, Timeout),
    # ── rejections: grooming (no retry) ─────────────────────────────────────────────
    *[rule for p in S.GROOMING_EPOCH for rule in _recovery_rules(p, Reject)],
    # ── rejections: implementing / testing / accepting (retry then recover) ─────────────────────────────────────────────
    *retry_epoch_rules(
        S.IMPLEMENTING, S.IMPLEMENTING_EPOCH, retry_target=S.IMPLEMENTING, recovering_stage=S.RECOVERING
    ),
    Rule(
        from_state=S.TESTING,
        on_event=Reject,
        transition_to=S.ACCEPTING,
        when=stage_retries_exhausted("testing") & last_hook_ok(),
        with_effect=remember_rejection("accepting"),
    ),
    *retry_epoch_rules(S.TESTING, S.TESTING_EPOCH, retry_target=S.IMPLEMENTING, recovering_stage=S.RECOVERING),
    *retry_epoch_rules(S.ACCEPTING, S.ACCEPTING_EPOCH, retry_target=S.IMPLEMENTING, recovering_stage=S.RECOVERING),
    # ── rejections: commit (no retry) ─────────────────────────────────────────────
    *[rule for p in S.COMMIT_EPOCH for rule in _recovery_rules(p, Reject)],
    # ── blocked ─────────────────────────────────────────────
    *_recovery_rules(S.GROOMING, Blocked),
    *_recovery_rules(S.IMPLEMENTING, Blocked),
    *_recovery_rules(S.TESTING, Blocked),
    *_recovery_rules(S.ACCEPTING, Blocked),
    # ── escalations ─────────────────────────────────────────────
    *_recovery_rules(S.ALL_STAGE_PHASES, StageRetryLimitHit),
    *_recovery_rules(S.ALL_STAGE_PHASES, OverallRetryLimitHit),
    # ── recovering ─────────────────────────────────────────────
    Rule(
        from_state=S.RECOVERING,
        on_event=RecoverySucceeded,
        transition_to=resume_from_origin,
        when=recovery_resume_is_concrete(),
        with_effect=clear_recovery_attempt,
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=RecoverySucceeded,
        transition_to=S.FAILED,
        with_effect=fail("recovery_missing_target_stage"),
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=RecoveryFailed,
        transition_to=S.FAILED,
        with_effect=fail("recovery_exhausted"),
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=RecoveryBudgetHit,
        transition_to=S.FAILED,
        with_effect=fail("recovery_budget_hit"),
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=Crash,
        transition_to=S.FAILED,
        with_effect=fail("recovery_crashed"),
    ),
    Rule(
        from_state=S.RECOVERING,
        on_event=Timeout,
        transition_to=S.FAILED,
        with_effect=fail("recovery_crashed"),
    ),
    # ── wildcards (must be last) ─────────────────────────────────────────────
    *_recovery_rules(S.ALL_STAGE_PHASES, Crash),
    *_recovery_rules(S.ALL_STAGE_PHASES, Timeout),
]
