"""Rule evaluation mechanics. No rules defined here — see ``rules.py``.

This module provides:
  - ``Rule`` / ``Transition`` dataclasses
  - ``evaluate()`` — the pure transition function
  - Helper functions used as callable ``transition_to`` targets
  - ``retry_epoch_rules()`` — generates retry/exhaust rule pairs
"""

from dataclasses import dataclass
from typing import Callable

from litehive.domain.lifecycle_deltas import (
    EMPTY_DELTA,
    EffectFn,
    StateDelta,
    fail,
    fail_rejection_loop,
    inc_stage_retry,
)
from litehive.domain.common import PipelineState, canonical_pipeline_state
from .events import Event, PreExecRecoverySucceeded, RecoverySucceeded, Reject
from .guards import (
    Guard,
    hook_reject_loop_detected,
    rejection_loop_detected,
    stage_retries_exhausted,
    stage_retries_remaining,
)
from .persistence import TaskState
from .stages import Stage
from .types import FailedReason, STAGES

ToFn = Callable[[TaskState, Event], PipelineState]
ToSpec = PipelineState | str | ToFn | Stage


def _entry_phase(stage: str | PipelineState) -> PipelineState:
    pipeline_state = canonical_pipeline_state(stage)
    if pipeline_state == PipelineState.COMMIT:
        return PipelineState.COMMIT
    return canonical_pipeline_state(f"before_{pipeline_state}")


@dataclass(frozen=True)
class Rule:
    from_state: PipelineState | str | frozenset | Stage
    on_event: type[Event]
    transition_to: ToSpec
    when: Guard | None = None
    with_effect: EffectFn | None = None
    description: str = ""


@dataclass(frozen=True)
class Transition:
    next: PipelineState
    delta: StateDelta
    rule: Rule


class NoTransitionError(RuntimeError):
    def __init__(self, current: str | PipelineState, event: Event) -> None:
        super().__init__(f"no transition rule matched: current={current!r} event={type(event).__name__}")
        self.current = current
        self.event = event


def _matches_from(pattern, current: PipelineState) -> bool:
    if isinstance(pattern, frozenset):
        return current in pattern
    if isinstance(pattern, Stage):
        return pattern.name == current
    return canonical_pipeline_state(pattern) == current


def _matches_event(pattern: type[Event], event: Event) -> bool:
    return isinstance(event, pattern)


def _resolve_to(to: ToSpec, state: TaskState, event: Event) -> PipelineState:
    if callable(to) and not isinstance(to, Stage):
        return canonical_pipeline_state(to(state, event))
    if isinstance(to, Stage):
        return to.name
    return canonical_pipeline_state(to)


def evaluate(rules: list[Rule], current: str | PipelineState, event: Event, state: TaskState) -> Transition:
    """First-match evaluation. Pure function — no I/O, no mutation."""
    current_state = canonical_pipeline_state(current)
    for rule in rules:
        if not _matches_from(rule.from_state, current_state):
            continue
        if not _matches_event(rule.on_event, event):
            continue
        if rule.when is not None and not rule.when(state, event):
            continue
        target = _resolve_to(rule.transition_to, state, event)
        if rule.with_effect is not None:
            delta = rule.with_effect(state, event)
        else:
            delta = EMPTY_DELTA
        return Transition(next=target, delta=delta, rule=rule)
    raise NoTransitionError(current, event)


# ── callable transition_to targets ──────────────────────────────────────


def resume_from_origin(state: TaskState, event: Event) -> PipelineState:
    e: RecoverySucceeded = event  # type: ignore[assignment]
    if e.resume == "done":
        return PipelineState.DONE
    if not e.resume:
        trigger = state.active_recovery_trigger
        if trigger is not None and trigger.origin_stage in STAGES:
            return _entry_phase(trigger.origin_stage)
        raise ValueError("RecoverySucceeded missing resume destination")
    if e.resume in STAGES:
        return _entry_phase(e.resume)
    return canonical_pipeline_state(e.resume)


def resume_from_pre_exec(state: TaskState, event: Event) -> PipelineState:
    e: PreExecRecoverySucceeded = event  # type: ignore[assignment]
    if e.resume_stage in STAGES:
        return _entry_phase(e.resume_stage)
    return canonical_pipeline_state(e.resume_stage)


def entry_from_worktree_sync(state: TaskState, event: Event) -> PipelineState:
    del event
    if state.entry_stage in STAGES:
        return _entry_phase(state.entry_stage)
    if state.entry_stage:
        return canonical_pipeline_state(state.entry_stage)
    if state.pipeline_mode == "single":
        return PipelineState.BEFORE_IMPLEMENTING
    return PipelineState.BEFORE_GROOMING


# ── rule generators (used by rules.py) ──────────────────────────────────


def retry_epoch_rules(counter_stage, phases, retry_target, exhausted_reason: FailedReason | str) -> list[Rule]:
    """Generate retry + fail rule pairs for a retryable epoch.

    ``counter_stage`` — the stage whose retry counter is checked/bumped.
    ``retry_target`` — where to go on retry (usually IMPLEMENTING).
    ``exhausted_reason`` — terminal failure reason when retries are exhausted.
    """
    if isinstance(counter_stage, Stage):
        name = counter_stage.name
    else:
        name = counter_stage
    if isinstance(retry_target, Stage):
        retry_target_name = retry_target.name
    else:
        retry_target_name = retry_target
    rules: list[Rule] = []
    for phase in phases:
        rules.append(
            Rule(
                from_state=phase,
                on_event=Reject,
                transition_to="failed",
                when=rejection_loop_detected(retry_target_name),
                with_effect=fail_rejection_loop(
                    name,
                    retry_target_stage=retry_target_name,
                ),
            )
        )
        rules.append(
            Rule(
                from_state=phase,
                on_event=Reject,
                transition_to="failed",
                when=hook_reject_loop_detected(),
                with_effect=fail(FailedReason.HOOK_REJECT_LOOP),
            )
        )
        rules.append(
            Rule(
                from_state=phase,
                on_event=Reject,
                transition_to=retry_target,
                when=stage_retries_remaining(name),
                with_effect=inc_stage_retry(
                    name,
                    retry_target_stage=retry_target_name,
                ),
            )
        )
        rules.append(
            Rule(
                from_state=phase,
                on_event=Reject,
                transition_to="failed",
                when=stage_retries_exhausted(name),
                with_effect=fail(exhausted_reason),
            )
        )
    return rules


def list_transitions() -> list[Rule]:
    """Return the default rule table from ``rules.py``."""
    # inline: rules.py top-level-imports transitions.py for ``Rule`` etc.
    from .rules import RULES  # noqa: PLC0415

    return list(RULES)
