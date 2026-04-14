"""The pipeline rule table. Nothing else lives here.

Read top to bottom to understand routing. Each row is:
    Rule(from_state=STATE, on_event=EVENT, transition_to=→ TARGET, guard?, effect?,)

Ctrl+click any S.STAGE to see the node that runs there.
"""

from litehive.domain.lifecycle_deltas import clear_recovery_attempt, enter_pre_exec_recovery, enter_recovery, fail, stash_conflict_files
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
from .guards import hook_reject_loop_detected, mode, zero_change_shortcut
from .stages import Stages as S
from .transitions import Rule, resume_from_origin, resume_from_pre_exec, retry_epoch_rules


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
        transition_to=S.BEFORE_GROOMING,
        when=mode("full"),
    ),
    Rule(
        from_state=S.WORKTREE_SYNC,
        on_event=Pass,
        transition_to=S.BEFORE_IMPLEMENTING,
        when=mode("single"),
    ),
    Rule(
        from_state=S.WORKTREE_SYNC,
        on_event=Reject,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    Rule(
        from_state=S.WORKTREE_SYNC,
        on_event=Crash,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    Rule(
        from_state=S.WORKTREE_SYNC,
        on_event=Timeout,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
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
        transition_to=S.BEFORE_COMMIT,
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
    ),
    Rule(
        from_state=S.AFTER_ACCEPTING,
        on_event=HookOk,
        transition_to=S.BEFORE_COMMIT,
    ),
    # ── commit ─────────────────────────────────────────────
    Rule(
        from_state=S.BEFORE_COMMIT,
        on_event=HookOk,
        transition_to=S.COMMIT,
    ),
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
    Rule(
        from_state=S.MERGE_RESOLVING,
        on_event=Reject,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    Rule(
        from_state=S.MERGE_RESOLVING,
        on_event=Blocked,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    Rule(
        from_state=S.MERGE_RESOLVING,
        on_event=Crash,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    Rule(
        from_state=S.MERGE_RESOLVING,
        on_event=Timeout,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    # ── rejections: grooming (no retry) ─────────────────────────────────────────────
    *[
        Rule(
            from_state=p,
            on_event=Reject,
            transition_to=S.RECOVERING,
            with_effect=enter_recovery,
        )
        for p in S.GROOMING_EPOCH
    ],
    # ── same-hook reject circuit breaker ─────────────────────────────────────────────
    *[
        Rule(
            from_state=phase,
            on_event=Reject,
            transition_to=S.RECOVERING,
            when=hook_reject_loop_detected(),
            with_effect=enter_recovery,
        )
        for phase in (*S.IMPLEMENTING_EPOCH, *S.TESTING_EPOCH, *S.ACCEPTING_EPOCH)
    ],
    # ── rejections: implementing / testing / accepting (retry then recover) ─────────────────────────────────────────────
    *retry_epoch_rules(
        S.IMPLEMENTING, S.IMPLEMENTING_EPOCH, retry_target=S.IMPLEMENTING, recovering_stage=S.RECOVERING
    ),
    *retry_epoch_rules(S.TESTING, S.TESTING_EPOCH, retry_target=S.IMPLEMENTING, recovering_stage=S.RECOVERING),
    *retry_epoch_rules(S.ACCEPTING, S.ACCEPTING_EPOCH, retry_target=S.IMPLEMENTING, recovering_stage=S.RECOVERING),
    # ── rejections: commit (no retry) ─────────────────────────────────────────────
    *[
        Rule(
            from_state=p,
            on_event=Reject,
            transition_to=S.RECOVERING,
            with_effect=enter_recovery,
        )
        for p in S.COMMIT_EPOCH
    ],
    # ── blocked ─────────────────────────────────────────────
    Rule(
        from_state=S.GROOMING,
        on_event=Blocked,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    Rule(
        from_state=S.IMPLEMENTING,
        on_event=Blocked,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    Rule(
        from_state=S.TESTING,
        on_event=Blocked,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    Rule(
        from_state=S.ACCEPTING,
        on_event=Blocked,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    # ── escalations ─────────────────────────────────────────────
    Rule(
        from_state=S.ALL_STAGE_PHASES,
        on_event=StageRetryLimitHit,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    Rule(
        from_state=S.ALL_STAGE_PHASES,
        on_event=OverallRetryLimitHit,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    # ── recovering ─────────────────────────────────────────────
    Rule(
        from_state=S.RECOVERING,
        on_event=RecoverySucceeded,
        transition_to=resume_from_origin,
        with_effect=clear_recovery_attempt,
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
    Rule(
        from_state=S.ALL_STAGE_PHASES,
        on_event=Crash,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
    Rule(
        from_state=S.ALL_STAGE_PHASES,
        on_event=Timeout,
        transition_to=S.RECOVERING,
        with_effect=enter_recovery,
    ),
]
