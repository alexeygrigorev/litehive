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
    """One row of the transition table.

    Reads like English: "from <from_state> on <on_event> transition_to
    <transition_to> when <when> with_effect <with_effect>".
    """

    from_state: NodeName | frozenset[NodeName]
    on_event: type[Event]
    transition_to: ToSpec
    when: Guard | None = None
    with_effect: EffectFn | None = None
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
        if not _matches_from(rule.from_state, current):
            continue
        if not _matches_event(rule.on_event, event):
            continue
        if rule.when is not None and not rule.when(state, event):
            continue
        target = (
            rule.transition_to(state, event)
            if callable(rule.transition_to)
            else rule.transition_to
        )
        delta = (
            rule.with_effect(state, event) if rule.with_effect is not None else EMPTY_DELTA
        )
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
                from_state=phase,
                on_event=Reject,
                transition_to="implementing",
                when=stage_retries_remaining(epoch_stage),
                with_effect=inc_stage_retry(epoch_stage),
                description=f"{phase} reject → implementing (retry {epoch_stage})",
            )
        )
        rules.append(
            Rule(
                from_state=phase,
                on_event=Reject,
                transition_to="recovering",
                when=stage_retries_exhausted(epoch_stage),
                with_effect=enter_recovery,
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
    Rule(from_state="ready", on_event=CleanState, transition_to="before_grooming",
         when=mode("full"),
         description="ready → grooming entry (full mode)"),
    Rule(from_state="ready", on_event=CleanState, transition_to="before_implementing",
         when=mode("single"),
         description="ready → implementing entry (single mode)"),
    Rule(from_state="ready", on_event=NeedsPreExecRecovery, transition_to="recovering_pre_exec",
         with_effect=enter_pre_exec_recovery,
         description="ready → pre-exec recovery"),

    # ── pre-execution recovery exits ────────────────────────────────────
    Rule(from_state="recovering_pre_exec", on_event=PreExecRecoverySucceeded, transition_to=resume_from_pre_exec,
         description="pre-exec recovery resumes at origin"),
    Rule(from_state="recovering_pre_exec", on_event=PreExecRecoveryFailed, transition_to="failed",
         with_effect=fail("pre_exec_recovery_failed")),
    Rule(from_state="recovering_pre_exec", on_event=PreExecRecoveryBudgetHit, transition_to="failed",
         with_effect=fail("pre_exec_recovery_failed")),
    Rule(from_state="recovering_pre_exec", on_event=Crash, transition_to="failed",
         with_effect=fail("recovery_crashed")),
    Rule(from_state="recovering_pre_exec", on_event=Timeout, transition_to="failed",
         with_effect=fail("recovery_crashed")),

    # ── happy path: grooming ────────────────────────────────────────────
    Rule(from_state="before_grooming", on_event=HookOk, transition_to="grooming"),
    Rule(from_state="grooming",        on_event=Pass,   transition_to="after_grooming"),
    Rule(from_state="after_grooming",  on_event=HookOk, transition_to="before_implementing"),

    # ── happy path: implementing ────────────────────────────────────────
    Rule(from_state="before_implementing", on_event=HookOk, transition_to="implementing"),
    Rule(from_state="implementing",        on_event=Pass,   transition_to="after_implementing"),

    # after_implementing: mode-gated exits, most specific first
    Rule(from_state="after_implementing", on_event=HookOk, transition_to="done",
         when=mode("single") & zero_change_shortcut(),
         description="single mode + no diff → skip commit"),
    Rule(from_state="after_implementing", on_event=HookOk, transition_to="before_commit",
         when=mode("single")),
    Rule(from_state="after_implementing", on_event=HookOk, transition_to="before_testing",
         when=mode("full")),

    # ── happy path: testing ─────────────────────────────────────────────
    Rule(from_state="before_testing", on_event=HookOk, transition_to="testing"),
    Rule(from_state="testing",        on_event=Pass,   transition_to="after_testing"),
    Rule(from_state="after_testing",  on_event=HookOk, transition_to="before_accepting"),

    # ── happy path: accepting ───────────────────────────────────────────
    Rule(from_state="before_accepting", on_event=HookOk, transition_to="accepting"),
    Rule(from_state="accepting",        on_event=Pass,   transition_to="after_accepting"),
    Rule(from_state="after_accepting",  on_event=HookOk, transition_to="before_commit"),

    # ── happy path: commit ──────────────────────────────────────────────
    Rule(from_state="before_commit", on_event=HookOk, transition_to="commit"),
    Rule(from_state="commit",        on_event=Pass,   transition_to="after_commit"),
    Rule(from_state="after_commit",  on_event=HookOk, transition_to="done"),

    # ── rejections: grooming epoch (no self-retry) ──────────────────────
    *[Rule(from_state=p, on_event=Reject, transition_to="recovering",
           with_effect=enter_recovery,
           description=f"{p} reject → recovering") for p in GROOMING_EPOCH],

    # ── rejections: implementing / testing / accepting epochs ───────────
    *_retry_epoch_rules("implementing", IMPLEMENTING_EPOCH),
    *_retry_epoch_rules("testing",      TESTING_EPOCH),
    *_retry_epoch_rules("accepting",    ACCEPTING_EPOCH),

    # ── commit: merge conflict → merge agent (one shot) ─────────────────
    Rule(from_state="commit", on_event=MergeConflictDetected, transition_to="merge_resolving",
         with_effect=stash_conflict_files,
         description="commit conflict → merge_resolving (MergeAgent)"),
    Rule(from_state="merge_resolving", on_event=Pass, transition_to="after_commit",
         description="merge agent resolved + committed → continue"),
    Rule(from_state="merge_resolving", on_event=Reject, transition_to="recovering",
         with_effect=enter_recovery,
         description="merge agent gave up → recovering"),
    Rule(from_state="merge_resolving", on_event=Blocked, transition_to="recovering",
         with_effect=enter_recovery),
    Rule(from_state="merge_resolving", on_event=Crash, transition_to="recovering",
         with_effect=enter_recovery),
    Rule(from_state="merge_resolving", on_event=Timeout, transition_to="recovering",
         with_effect=enter_recovery),

    # ── rejections: commit epoch (no self-retry) ────────────────────────
    *[Rule(from_state=p, on_event=Reject, transition_to="recovering",
           with_effect=enter_recovery,
           description=f"{p} reject → recovering") for p in COMMIT_EPOCH],

    # ── blocked from any agent stage ────────────────────────────────────
    Rule(from_state="grooming",     on_event=Blocked, transition_to="recovering", with_effect=enter_recovery),
    Rule(from_state="implementing", on_event=Blocked, transition_to="recovering", with_effect=enter_recovery),
    Rule(from_state="testing",      on_event=Blocked, transition_to="recovering", with_effect=enter_recovery),
    Rule(from_state="accepting",    on_event=Blocked, transition_to="recovering", with_effect=enter_recovery),

    # ── runner-emitted escalations ──────────────────────────────────────
    Rule(from_state=ANY_STAGE_PHASE, on_event=StageRetryLimitHit,   transition_to="recovering",
         with_effect=enter_recovery),
    Rule(from_state=ANY_STAGE_PHASE, on_event=OverallRetryLimitHit, transition_to="recovering",
         with_effect=enter_recovery),

    # ── recovering: exits (specific first, so wildcards below don't win) ─
    Rule(from_state="recovering", on_event=RecoverySucceeded, transition_to=resume_from_origin,
         with_effect=clear_recovery_attempt,
         description="recovery succeeded → resume at origin or target"),
    Rule(from_state="recovering", on_event=RecoveryFailed,    transition_to="failed",
         with_effect=fail("recovery_exhausted")),
    Rule(from_state="recovering", on_event=RecoveryBudgetHit, transition_to="failed",
         with_effect=fail("recovery_budget_hit")),
    Rule(from_state="recovering", on_event=Crash,             transition_to="failed",
         with_effect=fail("recovery_crashed")),
    Rule(from_state="recovering", on_event=Timeout,           transition_to="failed",
         with_effect=fail("recovery_crashed")),

    # ── tier-3 wildcards ────────────────────────────────────────────────
    Rule(from_state=ANY_STAGE_PHASE, on_event=Crash,   transition_to="recovering",
         with_effect=enter_recovery,
         description="crash in any stage phase → recovering"),
    Rule(from_state=ANY_STAGE_PHASE, on_event=Timeout, transition_to="recovering",
         with_effect=enter_recovery,
         description="timeout in any stage phase → recovering"),
]


def list_transitions() -> list[Rule]:
    """Return the default rule table. Used by CLI inspection and tests."""
    return list(RULES)
