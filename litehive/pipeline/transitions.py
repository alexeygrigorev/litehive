from dataclasses import dataclass
from typing import Callable, Iterable

from .deltas import (
    EMPTY_DELTA,
    EffectFn,
    StateDelta,
    clear_recovery_attempt,
    enter_pre_exec_recovery,
    enter_recovery,
    fail,
    inc_stage_retry,
    stash_conflict_files,
)
from .events import (
    Blocked,
    CleanState,
    Crash,
    Event,
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
    Guard,
    mode,
    stage_retries_exhausted,
    stage_retries_remaining,
    zero_change_shortcut,
)
from .persistence import TaskState
from .types import ANY_STAGE_PHASE, NodeName, STAGES

ToFn = Callable[[TaskState, Event], NodeName]
ToSpec = NodeName | ToFn


@dataclass(frozen=True)
class Rule:
    from_: NodeName | frozenset[NodeName]
    event: type[Event]
    to: ToSpec
    when: Guard | None = None
    effect: EffectFn | None = None
    description: str = ""


@dataclass(frozen=True)
class Transition:
    next: NodeName
    delta: StateDelta
    rule: Rule


class NoTransitionError(RuntimeError):
    def __init__(self, current: NodeName, event: Event) -> None:
        super().__init__(
            f"no transition rule matched: current={current!r} event={type(event).__name__}"
        )
        self.current = current
        self.event = event


def _matches_from(pattern: NodeName | frozenset[NodeName], current: NodeName) -> bool:
    if isinstance(pattern, frozenset):
        return current in pattern
    return pattern == current


def _matches_event(pattern: type[Event], event: Event) -> bool:
    return isinstance(event, pattern)


def evaluate(
    rules: list[Rule], current: NodeName, event: Event, state: TaskState
) -> Transition:
    """Find the first rule matching (current, event, state) and return its Transition.

    Pure function. The ``StateMachineRunner`` uses this; tests can call it
    directly without any runner, registry, or persistence.
    """
    for rule in rules:
        if not _matches_from(rule.from_, current):
            continue
        if not _matches_event(rule.event, event):
            continue
        if rule.when is not None and not rule.when(state, event):
            continue
        target = rule.to(state, event) if callable(rule.to) else rule.to
        delta = rule.effect(state, event) if rule.effect is not None else EMPTY_DELTA
        return Transition(next=target, delta=delta, rule=rule)
    raise NoTransitionError(current, event)


def resume_from_origin(state: TaskState, event: Event) -> NodeName:
    e: RecoverySucceeded = event  # type: ignore[assignment]
    if e.resume == "done":
        return "done"
    if e.resume in STAGES:
        return f"before_{e.resume}"
    return e.resume


def resume_from_pre_exec(state: TaskState, event: Event) -> NodeName:
    e: PreExecRecoverySucceeded = event  # type: ignore[assignment]
    if e.resume_stage in STAGES:
        return f"before_{e.resume_stage}"
    return e.resume_stage


def _retry_epoch_rules(epoch_stage: NodeName, phases: Iterable[NodeName]) -> list[Rule]:
    rules: list[Rule] = []
    for phase in phases:
        rules.append(
            Rule(
                phase,
                Reject,
                "implementing",
                when=stage_retries_remaining(epoch_stage),
                effect=inc_stage_retry(epoch_stage),
                description=f"{phase} reject → implementing (retry {epoch_stage})",
            )
        )
        rules.append(
            Rule(
                phase,
                Reject,
                "recovering",
                when=stage_retries_exhausted(epoch_stage),
                effect=enter_recovery,
                description=f"{phase} reject → recovering ({epoch_stage} exhausted)",
            )
        )
    return rules


GROOMING_EPOCH      = ("before_grooming",      "grooming",      "after_grooming")
IMPLEMENTING_EPOCH  = ("before_implementing",  "implementing",  "after_implementing")
TESTING_EPOCH       = ("before_testing",       "testing",       "after_testing")
ACCEPTING_EPOCH     = ("before_accepting",     "accepting",     "after_accepting")
COMMIT_EPOCH        = ("before_commit",        "commit",        "after_commit")


RULES: list[Rule] = [
    # ── pre-execution entry ─────────────────────────────────────────────
    Rule("ready", CleanState, "before_grooming",
         when=mode("full"),
         description="ready → grooming entry (full mode)"),
    Rule("ready", CleanState, "before_implementing",
         when=mode("single"),
         description="ready → implementing entry (single mode)"),
    Rule("ready", NeedsPreExecRecovery, "recovering_pre_exec",
         effect=enter_pre_exec_recovery,
         description="ready → pre-exec recovery"),

    # ── pre-execution recovery exits ────────────────────────────────────
    Rule("recovering_pre_exec", PreExecRecoverySucceeded, resume_from_pre_exec,
         description="pre-exec recovery resumes at origin"),
    Rule("recovering_pre_exec", PreExecRecoveryFailed, "failed",
         effect=fail("pre_exec_recovery_failed")),
    Rule("recovering_pre_exec", PreExecRecoveryBudgetHit, "failed",
         effect=fail("pre_exec_recovery_failed")),
    Rule("recovering_pre_exec", Crash, "failed",
         effect=fail("recovery_crashed")),
    Rule("recovering_pre_exec", Timeout, "failed",
         effect=fail("recovery_crashed")),

    # ── happy path: grooming ────────────────────────────────────────────
    Rule("before_grooming",    HookOk, "grooming"),
    Rule("grooming",           Pass,   "after_grooming"),
    Rule("after_grooming",     HookOk, "before_implementing"),

    # ── happy path: implementing ────────────────────────────────────────
    Rule("before_implementing", HookOk, "implementing"),
    Rule("implementing",        Pass,   "after_implementing"),

    # after_implementing: mode-gated exits, most specific first
    Rule("after_implementing", HookOk, "done",
         when=mode("single") & zero_change_shortcut(),
         description="single mode + no diff → skip commit"),
    Rule("after_implementing", HookOk, "before_commit",
         when=mode("single")),
    Rule("after_implementing", HookOk, "before_testing",
         when=mode("full")),

    # ── happy path: testing ─────────────────────────────────────────────
    Rule("before_testing", HookOk, "testing"),
    Rule("testing",        Pass,   "after_testing"),
    Rule("after_testing",  HookOk, "before_accepting"),

    # ── happy path: accepting ───────────────────────────────────────────
    Rule("before_accepting", HookOk, "accepting"),
    Rule("accepting",        Pass,   "after_accepting"),
    Rule("after_accepting",  HookOk, "before_commit"),

    # ── happy path: commit ──────────────────────────────────────────────
    Rule("before_commit", HookOk, "commit"),
    Rule("commit",        Pass,   "after_commit"),
    Rule("after_commit",  HookOk, "done"),

    # ── rejections: grooming epoch (no self-retry) ──────────────────────
    *[Rule(p, Reject, "recovering", effect=enter_recovery,
           description=f"{p} reject → recovering") for p in GROOMING_EPOCH],

    # ── rejections: implementing / testing / accepting epochs ───────────
    *_retry_epoch_rules("implementing", IMPLEMENTING_EPOCH),
    *_retry_epoch_rules("testing",      TESTING_EPOCH),
    *_retry_epoch_rules("accepting",    ACCEPTING_EPOCH),

    # ── commit: merge conflict → merge agent (one shot) ─────────────────
    Rule("commit", MergeConflictDetected, "merge_resolving",
         effect=stash_conflict_files,
         description="commit conflict → merge_resolving (MergeAgent)"),
    Rule("merge_resolving", Pass, "after_commit",
         description="merge agent resolved + committed → continue"),
    Rule("merge_resolving", Reject, "recovering", effect=enter_recovery,
         description="merge agent gave up → recovering"),
    Rule("merge_resolving", Blocked, "recovering", effect=enter_recovery),
    Rule("merge_resolving", Crash, "recovering", effect=enter_recovery),
    Rule("merge_resolving", Timeout, "recovering", effect=enter_recovery),

    # ── rejections: commit epoch (no self-retry) ────────────────────────
    *[Rule(p, Reject, "recovering", effect=enter_recovery,
           description=f"{p} reject → recovering") for p in COMMIT_EPOCH],

    # ── blocked from any agent stage ────────────────────────────────────
    Rule("grooming",     Blocked, "recovering", effect=enter_recovery),
    Rule("implementing", Blocked, "recovering", effect=enter_recovery),
    Rule("testing",      Blocked, "recovering", effect=enter_recovery),
    Rule("accepting",    Blocked, "recovering", effect=enter_recovery),

    # ── runner-emitted escalations ──────────────────────────────────────
    Rule(ANY_STAGE_PHASE, StageRetryLimitHit,   "recovering", effect=enter_recovery),
    Rule(ANY_STAGE_PHASE, OverallRetryLimitHit, "recovering", effect=enter_recovery),

    # ── recovering: exits (specific first, so wildcards below don't win) ─
    Rule("recovering", RecoverySucceeded, resume_from_origin,
         effect=clear_recovery_attempt,
         description="recovery succeeded → resume at origin or target"),
    Rule("recovering", RecoveryFailed,    "failed", effect=fail("recovery_exhausted")),
    Rule("recovering", RecoveryBudgetHit, "failed", effect=fail("recovery_budget_hit")),
    Rule("recovering", Crash,             "failed", effect=fail("recovery_crashed")),
    Rule("recovering", Timeout,           "failed", effect=fail("recovery_crashed")),

    # ── tier-3 wildcards ────────────────────────────────────────────────
    Rule(ANY_STAGE_PHASE, Crash,   "recovering", effect=enter_recovery,
         description="crash in any stage phase → recovering"),
    Rule(ANY_STAGE_PHASE, Timeout, "recovering", effect=enter_recovery,
         description="timeout in any stage phase → recovering"),
]


def list_transitions() -> list[Rule]:
    """Return the default rule table. Used by CLI inspection and tests."""
    return list(RULES)
