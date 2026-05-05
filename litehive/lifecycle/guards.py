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
        """Evaluate the guard's predicate against (state, event); used by the rule engine when matching transitions."""
        return self.fn(state, event)

    def __and__(self, other: "Guard") -> "Guard":
        """Compose two guards into one that fires only when both hold; lets rule rows write `mode("single") & zero_change_shortcut()`."""
        left, right = self.fn, other.fn

        def both(state: TaskState, event: Event) -> bool:
            return left(state, event) and right(state, event)

        return Guard(both, f"({self.description} AND {other.description})")

    def __or__(self, other: "Guard") -> "Guard":
        """Compose two guards into one that fires when either holds; used by rule rows that share a transition under alternative conditions."""
        left, right = self.fn, other.fn

        def either(state: TaskState, event: Event) -> bool:
            return left(state, event) or right(state, event)

        return Guard(either, f"({self.description} OR {other.description})")

    def __invert__(self) -> "Guard":
        """Negate a guard so the rule table can express the complement (e.g. `recovery_budget_exhausted = ~recovery_budget_available`) without duplicating predicate logic."""
        inner = self.fn

        def negated(state: TaskState, event: Event) -> bool:
            return not inner(state, event)

        return Guard(negated, f"NOT ({self.description})")


def mode(m: PipelineMode | str) -> Guard:
    """True when the task's pipeline mode matches `m`. Used by the after_implementing rule fan-out, where `single` skips the testing/accepting stages and `full` keeps them."""
    if isinstance(m, PipelineMode):
        want = m
    else:
        want = PipelineMode(m)

    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.pipeline_mode == want

    return Guard(check, f"mode={want.value}")


def stage_retries_remaining(stage: PipelineState) -> Guard:
    """True when this stage still has reject-retry attempts left under the per-stage retry limit. Gates the retry-target rule emitted by `retry_epoch_rules` so a Reject loops the task back to the configured stage instead of failing it."""
    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.stage_retry.get(stage, 0) < state.limits.stage_retry_limit

    return Guard(check, f"stage_retries_remaining({stage})")


def stage_retries_exhausted(stage: PipelineState) -> Guard:
    """True once this stage has burned its full reject-retry budget. Combined with `last_hook_ok` to escape a stuck testing stage by jumping to accepting instead of failing the task outright."""
    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.stage_retry.get(stage, 0) >= state.limits.stage_retry_limit

    return Guard(check, f"stage_retries_exhausted({stage})")


def last_hook_ok() -> Guard:
    """True when the most recent hook report passed. Pairs with `stage_retries_exhausted` so we only override testing rejects when the hook itself was happy — semantic-only failure, not a broken pipeline."""
    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.last_report.hook_ok is True

    return Guard(check, "last_hook_ok")


def hook_reject_loop_detected() -> Guard:
    """True when the same hook has rejected the task `same_hook_reject_limit` times in a row. Used by `retry_epoch_rules` to fail the task on a hook livelock instead of looping forever."""
    def check(state: TaskState, event: Event) -> bool:
        if not isinstance(event, Reject) or event.source != "hook":
            return False
        count = event.metadata.get("consecutive_same_hook_rejects")
        return isinstance(count, int) and count >= state.limits.same_hook_reject_limit

    return Guard(check, "hook_reject_loop_detected")


def rejection_loop_detected(retry_target_stage: PipelineState) -> Guard:
    """True when reviewer rejects keep cycling back to the same retry target without progress. Used by `retry_epoch_rules` to fail the task with `rejection_loop_detected` instead of retrying the same stage indefinitely."""
    def check(state: TaskState, event: Event) -> bool:
        return rejection_loop_detected_delta(state, event, retry_target_stage=retry_target_stage)

    return Guard(check, f"rejection_loop_detected({retry_target_stage})")


def zero_change_shortcut() -> Guard:
    """True when the implementing stage produced no file changes and no new tests. Lets `single`-mode tasks short-circuit straight to DONE instead of running an empty commit through the merge pipeline."""
    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.last_report.files_changed == 0 and state.last_report.tests_added == 0

    return Guard(check, "zero_change_shortcut")


def pre_exec_budget_remaining() -> Guard:
    """True while the task has not yet used its single pre-exec recovery attempt. Reserved for a pre-exec entry rule; not currently wired into the rule table."""
    def check(state: TaskState, event: Event) -> bool:
        del event
        return state.pre_exec_recovery_attempt < 1

    return Guard(check, "pre_exec_budget_remaining")


def recovery_budget_available() -> Guard:
    """True when the recovery trigger inferred from the event still has budget left. Gates the `_recovery_rules` row that routes Crash/Timeout/Blocked into RECOVERING instead of FAILED."""
    def check(state: TaskState, event: Event) -> bool:
        return state.recovery_budget_available(recovery_trigger_from_event(state, event))

    return Guard(check, "recovery_budget_available")


def recovery_budget_exhausted() -> Guard:
    """True when the recovery trigger inferred from the event has no budget left. Gates the `_recovery_rules` row that routes Crash/Timeout/Blocked straight to FAILED."""
    available = recovery_budget_available()
    return ~available


def recovery_resume_is_concrete() -> Guard:
    """True when the recovery agent reported a non-empty resume target. Gates the RECOVERING→resume rule so a vague success report falls through to the FAILED rule with `recovery_missing_target_stage`."""
    def check(state: TaskState, event: Event) -> bool:
        del state
        return isinstance(event, RecoverySucceeded) and bool(event.resume.strip())

    return Guard(check, "recovery_resume_is_concrete")


def _always(state: TaskState, event: Event) -> bool:
    """Predicate body for the module-level `always` guard; backs unconditional rule rows that need a Guard object for uniformity."""
    del state, event
    return True


always = Guard(_always, "always")
