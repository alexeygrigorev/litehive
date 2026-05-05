from dataclasses import dataclass
from typing import Callable

from litehive.domain.lifecycle_deltas import (
    recovery_trigger_from_event,
    rejection_loop_detected as rejection_loop_detected_delta,
)
from litehive.domain.common import PipelineState
from .events import Event, RecoverySucceeded, Reject
from .persistence import TaskState
from .types import PipelineMode

GuardFn = Callable[[TaskState, Event], bool]


@dataclass(frozen=True)
class Guard:
    """Composable predicate used by transition rules."""

    fn: GuardFn
    description: str = ""

    def __call__(self, state: TaskState, event: Event) -> bool:
        """Evaluate the predicate during rule matching."""
        return self.fn(state, event)

    def __and__(self, other: "Guard") -> "Guard":
        """
        Build a guard that fires only when both inputs do.

        Lets rule rows compose conjunctions inline (``mode("single") &
        zero_change_shortcut()``) without writing a named helper for every
        pair. The combinator is short-circuit so the right-hand predicate
        is skipped when the left-hand one already rejected the (state,
        event) pair.
        """
        left, right = self.fn, other.fn

        def both(state: TaskState, event: Event) -> bool:
            return left(state, event) and right(state, event)

        return Guard(both, f"({self.description} AND {other.description})")

    def __or__(self, other: "Guard") -> "Guard":
        """
        Build a guard that fires when either input does.

        Used by rule rows where two distinct conditions should reach the
        same transition target; short-circuits so the right-hand predicate
        is only evaluated when the left-hand one rejected.
        """
        left, right = self.fn, other.fn

        def either(state: TaskState, event: Event) -> bool:
            return left(state, event) or right(state, event)

        return Guard(either, f"({self.description} OR {other.description})")

    def __invert__(self) -> "Guard":
        """
        Build the complement of this guard.

        Lets the rule table express paired conditions like
        ``recovery_budget_exhausted = ~recovery_budget_available`` without
        a second predicate definition that could drift from the original.
        """
        inner = self.fn

        def negated(state: TaskState, event: Event) -> bool:
            return not inner(state, event)

        return Guard(negated, f"NOT ({self.description})")


def mode(m: PipelineMode | str) -> Guard:
    """
    Match the task's pipeline mode.

    Used by the ``after_implementing`` rule fan-out, where ``single``
    skips the testing/accepting stages and ``full`` keeps them, so the
    rule table can branch on mode without introducing a separate state.
    """
    if isinstance(m, PipelineMode):
        want = m
    else:
        want = PipelineMode(m)

    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.pipeline_mode == want

    return Guard(check, f"mode={want.value}")


def stage_retries_remaining(stage: PipelineState) -> Guard:
    """
    Match while the stage still has reject-retry attempts left.

    Gates the retry-target rule emitted by ``retry_epoch_rules`` so a
    Reject loops the task back to the configured stage instead of
    failing it as soon as the very first reject lands.
    """
    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.stage_retry.get(stage, 0) < state.limits.stage_retry_limit

    return Guard(check, f"stage_retries_remaining({stage})")


def stage_retries_exhausted(stage: PipelineState) -> Guard:
    """
    Match once the stage has burned its full reject-retry budget.

    Combined with ``last_hook_ok`` to escape a stuck testing stage by
    jumping to accepting instead of failing the task outright — when
    hooks pass, the QA rejection is treated as semantic-only and the
    reviewer gets a chance to override.
    """
    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.stage_retry.get(stage, 0) >= state.limits.stage_retry_limit

    return Guard(check, f"stage_retries_exhausted({stage})")


def last_hook_ok() -> Guard:
    """
    Match when the most recent hook report passed.

    Pairs with ``stage_retries_exhausted`` so we only override testing
    rejects when the hook itself was happy — i.e. the failure is
    semantic-only, not a broken pipeline that should be failed
    outright.
    """
    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.last_report.hook_ok is True

    return Guard(check, "last_hook_ok")


def hook_reject_loop_detected() -> Guard:
    """
    Match when the same hook has rejected ``same_hook_reject_limit``
    times in a row.

    Used by ``retry_epoch_rules`` to fail the task on a hook livelock
    instead of looping forever — once a hook has rejected the same
    stage repeatedly, no amount of further retry is going to land it.
    """
    def check(state: TaskState, event: Event) -> bool:
        if not isinstance(event, Reject) or event.source != "hook":
            return False
        count = event.metadata.get("consecutive_same_hook_rejects")
        return isinstance(count, int) and count >= state.limits.same_hook_reject_limit

    return Guard(check, "hook_reject_loop_detected")


def rejection_loop_detected(retry_target_stage: PipelineState) -> Guard:
    """
    Match when reviewer rejects keep cycling back to the same retry
    target without progress.

    Used by ``retry_epoch_rules`` to fail the task with
    ``rejection_loop_detected`` instead of retrying indefinitely. The
    actual loop heuristic lives in ``lifecycle_deltas`` so guard and
    effect can stay in sync — this wrapper just adapts it to the Guard
    protocol.
    """
    def check(state: TaskState, event: Event) -> bool:
        return rejection_loop_detected_delta(state, event, retry_target_stage=retry_target_stage)

    return Guard(check, f"rejection_loop_detected({retry_target_stage})")


def zero_change_shortcut() -> Guard:
    """
    Match when the implementing stage produced no file changes and no
    new tests.

    Lets ``single``-mode tasks short-circuit straight to DONE instead
    of running an empty commit through the merge pipeline, so a SWE
    that decides the task is already satisfied does not produce a noise
    commit.
    """
    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.last_report.files_changed == 0 and state.last_report.tests_added == 0

    return Guard(check, "zero_change_shortcut")


def pre_exec_budget_remaining() -> Guard:
    """
    Match while the task has not yet used its single pre-exec recovery
    attempt.

    Reserved for a pre-exec entry rule; the rule table currently
    enforces the one-attempt budget inside ``PreExecRecoveryNode``
    itself, but the guard exists so the rule shape can move without a
    separate predicate to define.
    """
    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.pre_exec_recovery_attempt < 1

    return Guard(check, "pre_exec_budget_remaining")


def recovery_budget_available() -> Guard:
    """
    Match when the recovery trigger inferred from the event still has
    budget left.

    Gates the ``_recovery_rules`` row that routes Crash/Timeout/Blocked
    into RECOVERING instead of FAILED. Each unique failure fingerprint
    gets one shot at recovery; once that shot is used, the matching
    guard flips and the partner rule fails the task instead.
    """
    def check(state: TaskState, event: Event) -> bool:
        return state.recovery_budget_available(recovery_trigger_from_event(state, event))

    return Guard(check, "recovery_budget_available")


def recovery_budget_exhausted() -> Guard:
    """
    Match when the recovery trigger inferred from the event has no
    budget left.

    Gates the ``_recovery_rules`` row that routes Crash/Timeout/Blocked
    straight to FAILED. Defined as the inverse of
    ``recovery_budget_available`` so the two rule rows can never
    disagree about whether budget remains.
    """
    available = recovery_budget_available()
    return ~available


def recovery_resume_is_concrete() -> Guard:
    """
    Match when ``RecoverySucceeded`` carries a non-empty resume target.

    Gates the RECOVERING→resume rule so a vague success report falls
    through to the FAILED rule with ``recovery_missing_target_stage``
    instead of resuming somewhere unspecified.
    """
    def check(state: TaskState, event: Event) -> bool:
        del state
        return isinstance(event, RecoverySucceeded) and bool(event.resume.strip())

    return Guard(check, "recovery_resume_is_concrete")


def _always(state: TaskState, event: Event) -> bool:
    """Backing predicate for the module-level ``always`` guard, used by rule rows that need a Guard object but no real condition."""
    del state, event
    return True


always = Guard(_always, "always")
