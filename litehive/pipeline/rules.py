"""The pipeline rule table. Nothing else lives here.

Read top to bottom to understand routing. Each row is:
    Rule(STATE,  EVENT,  → TARGET,  guard?,  effect?)

Ctrl+click any S.STAGE to see the node that runs there.
"""

from .deltas import (
    clear_recovery_attempt,
    enter_pre_exec_recovery,
    enter_recovery,
    fail,
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
from .guards import mode, zero_change_shortcut
from .stages import Stages as S
from .transitions import Rule, resume_from_origin, resume_from_pre_exec, retry_epoch_rules

# fmt: off
# ─────────────────────────── STATE                EVENT                        → TARGET ──────────────────── GUARD / EFFECT ─────────────────────────

RULES: list[Rule] = [

    # ── entry ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.READY,              CleanState,               S.WORKTREE_SYNC),
    Rule(S.READY,              NeedsPreExecRecovery,     S.PRE_EXEC,                                          with_effect=enter_pre_exec_recovery),

    # ── worktree sync ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.WORKTREE_SYNC,      Pass,                     S.BEFORE_GROOMING,         when=mode("full")),
    Rule(S.WORKTREE_SYNC,      Pass,                     S.BEFORE_IMPLEMENTING,     when=mode("single")),
    Rule(S.WORKTREE_SYNC,      Reject,                   S.RECOVERING,                                        with_effect=enter_recovery),
    Rule(S.WORKTREE_SYNC,      Crash,                    S.RECOVERING,                                        with_effect=enter_recovery),
    Rule(S.WORKTREE_SYNC,      Timeout,                  S.RECOVERING,                                        with_effect=enter_recovery),

    # ── pre-exec recovery ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.PRE_EXEC,           PreExecRecoverySucceeded, resume_from_pre_exec),
    Rule(S.PRE_EXEC,           PreExecRecoveryFailed,    S.FAILED,                                            with_effect=fail("pre_exec_recovery_failed")),
    Rule(S.PRE_EXEC,           PreExecRecoveryBudgetHit, S.FAILED,                                            with_effect=fail("pre_exec_recovery_failed")),
    Rule(S.PRE_EXEC,           Crash,                    S.FAILED,                                            with_effect=fail("recovery_crashed")),
    Rule(S.PRE_EXEC,           Timeout,                  S.FAILED,                                            with_effect=fail("recovery_crashed")),

    # ── grooming ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.BEFORE_GROOMING,    HookOk,                   S.GROOMING),
    Rule(S.GROOMING,           Pass,                     S.AFTER_GROOMING),
    Rule(S.AFTER_GROOMING,     HookOk,                   S.BEFORE_IMPLEMENTING),

    # ── implementing ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.BEFORE_IMPLEMENTING, HookOk,                  S.IMPLEMENTING),
    Rule(S.IMPLEMENTING,       Pass,                     S.AFTER_IMPLEMENTING),

    Rule(S.AFTER_IMPLEMENTING, HookOk,                   S.DONE,                    when=mode("single") & zero_change_shortcut()),
    Rule(S.AFTER_IMPLEMENTING, HookOk,                   S.BEFORE_COMMIT,           when=mode("single")),
    Rule(S.AFTER_IMPLEMENTING, HookOk,                   S.BEFORE_TESTING,          when=mode("full")),

    # ── testing ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.BEFORE_TESTING,     HookOk,                   S.TESTING),
    Rule(S.TESTING,            Pass,                     S.AFTER_TESTING),
    Rule(S.AFTER_TESTING,      HookOk,                   S.BEFORE_ACCEPTING),

    # ── accepting ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.BEFORE_ACCEPTING,   HookOk,                   S.ACCEPTING),
    Rule(S.ACCEPTING,          Pass,                     S.AFTER_ACCEPTING),
    Rule(S.AFTER_ACCEPTING,    HookOk,                   S.BEFORE_COMMIT),

    # ── commit ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.BEFORE_COMMIT,      HookOk,                   S.COMMIT),
    Rule(S.COMMIT,             Pass,                     S.AFTER_COMMIT),
    Rule(S.AFTER_COMMIT,       HookOk,                   S.DONE),

    # ── merge conflict → merge agent ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.COMMIT,             MergeConflictDetected,    S.MERGE_RESOLVING,                                   with_effect=stash_conflict_files),
    Rule(S.MERGE_RESOLVING,    Pass,                     S.AFTER_COMMIT),
    Rule(S.MERGE_RESOLVING,    Reject,                   S.RECOVERING,                                        with_effect=enter_recovery),
    Rule(S.MERGE_RESOLVING,    Blocked,                  S.RECOVERING,                                        with_effect=enter_recovery),
    Rule(S.MERGE_RESOLVING,    Crash,                    S.RECOVERING,                                        with_effect=enter_recovery),
    Rule(S.MERGE_RESOLVING,    Timeout,                  S.RECOVERING,                                        with_effect=enter_recovery),

    # ── rejections: grooming (no retry) ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    *[Rule(p, Reject, S.RECOVERING, with_effect=enter_recovery) for p in S.GROOMING_EPOCH],

    # ── rejections: implementing / testing / accepting (retry then recover) ───────────────────────────────────────────────────────────────────────
    *retry_epoch_rules(S.IMPLEMENTING, S.IMPLEMENTING_EPOCH, retry_target=S.IMPLEMENTING, recovering_stage=S.RECOVERING),
    *retry_epoch_rules(S.TESTING,      S.TESTING_EPOCH,      retry_target=S.IMPLEMENTING, recovering_stage=S.RECOVERING),
    *retry_epoch_rules(S.ACCEPTING,    S.ACCEPTING_EPOCH,    retry_target=S.IMPLEMENTING, recovering_stage=S.RECOVERING),

    # ── rejections: commit (no retry) ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
    *[Rule(p, Reject, S.RECOVERING, with_effect=enter_recovery) for p in S.COMMIT_EPOCH],

    # ── blocked ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.GROOMING,           Blocked,                  S.RECOVERING,                                        with_effect=enter_recovery),
    Rule(S.IMPLEMENTING,       Blocked,                  S.RECOVERING,                                        with_effect=enter_recovery),
    Rule(S.TESTING,            Blocked,                  S.RECOVERING,                                        with_effect=enter_recovery),
    Rule(S.ACCEPTING,          Blocked,                  S.RECOVERING,                                        with_effect=enter_recovery),

    # ── escalations ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.ALL_STAGE_PHASES,   StageRetryLimitHit,       S.RECOVERING,                                        with_effect=enter_recovery),
    Rule(S.ALL_STAGE_PHASES,   OverallRetryLimitHit,     S.RECOVERING,                                        with_effect=enter_recovery),

    # ── recovering ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.RECOVERING,         RecoverySucceeded,        resume_from_origin,                                   with_effect=clear_recovery_attempt),
    Rule(S.RECOVERING,         RecoveryFailed,           S.FAILED,                                            with_effect=fail("recovery_exhausted")),
    Rule(S.RECOVERING,         RecoveryBudgetHit,        S.FAILED,                                            with_effect=fail("recovery_budget_hit")),
    Rule(S.RECOVERING,         Crash,                    S.FAILED,                                            with_effect=fail("recovery_crashed")),
    Rule(S.RECOVERING,         Timeout,                  S.FAILED,                                            with_effect=fail("recovery_crashed")),

    # ── wildcards (must be last) ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Rule(S.ALL_STAGE_PHASES,   Crash,                    S.RECOVERING,                                        with_effect=enter_recovery),
    Rule(S.ALL_STAGE_PHASES,   Timeout,                  S.RECOVERING,                                        with_effect=enter_recovery),
]
# fmt: on
