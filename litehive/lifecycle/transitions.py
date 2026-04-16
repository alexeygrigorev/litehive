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
    enter_recovery,
    exhaust_recovery_budget,
    inc_stage_retry,
)
from .events import Event, PreExecRecoverySucceeded, RecoverySucceeded, Reject
from .guards import Guard, recovery_budget_available, recovery_budget_exhausted, stage_retries_exhausted, stage_retries_remaining
from .persistence import TaskState
from .stages import Stage
from .types import STAGES, NodeName

ToFn = Callable[[TaskState, Event], NodeName]
ToSpec = NodeName | ToFn | Stage


@dataclass(frozen=True)
class Rule:
    from_state: NodeName | frozenset | Stage
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


def _matches_from(pattern, current: str) -> bool:
    if isinstance(pattern, frozenset):
        return current in pattern
    if isinstance(pattern, Stage):
        return pattern.name == current
    return pattern == current


def _matches_event(pattern: type[Event], event: Event) -> bool:
    return isinstance(event, pattern)


def _resolve_to(to: ToSpec, state: TaskState, event: Event) -> NodeName:
    if callable(to) and not isinstance(to, Stage):
        return to(state, event)
    if isinstance(to, Stage):
        return to.name
    return to


def evaluate(
    rules: list[Rule], current: NodeName, event: Event, state: TaskState
) -> Transition:
    """First-match evaluation. Pure function — no I/O, no mutation."""
    for rule in rules:
        if not _matches_from(rule.from_state, current):
            continue
        if not _matches_event(rule.on_event, event):
            continue
        if rule.when is not None and not rule.when(state, event):
            continue
        target = _resolve_to(rule.transition_to, state, event)
        delta = (
            rule.with_effect(state, event) if rule.with_effect is not None else EMPTY_DELTA
        )
        return Transition(next=target, delta=delta, rule=rule)
    raise NoTransitionError(current, event)


# ── callable transition_to targets ──────────────────────────────────────


def resume_from_origin(state: TaskState, event: Event) -> NodeName:
    e: RecoverySucceeded = event  # type: ignore[assignment]
    if e.resume == "done":
        return "done"
    if not e.resume:
        trigger = state.active_recovery_trigger
        if trigger is not None and trigger.origin_stage in STAGES:
            return f"before_{trigger.origin_stage}"
        raise ValueError("RecoverySucceeded missing resume destination")
    if e.resume in STAGES:
        return f"before_{e.resume}"
    return e.resume


def resume_from_pre_exec(state: TaskState, event: Event) -> NodeName:
    e: PreExecRecoverySucceeded = event  # type: ignore[assignment]
    if e.resume_stage in STAGES:
        return f"before_{e.resume_stage}"
    return e.resume_stage


# ── rule generators (used by rules.py) ──────────────────────────────────


def retry_epoch_rules(counter_stage, phases, retry_target, recovering_stage) -> list[Rule]:
    """Generate retry + exhaust rule pairs for a retryable epoch.

    ``counter_stage`` — the stage whose retry counter is checked/bumped.
    ``retry_target`` — where to go on retry (usually IMPLEMENTING).
    ``recovering_stage`` — where to go when retries are exhausted.
    """
    name = counter_stage.name if isinstance(counter_stage, Stage) else counter_stage
    rules: list[Rule] = []
    for phase in phases:
        rules.append(Rule(
            from_state=phase, on_event=Reject, transition_to=retry_target,
            when=stage_retries_remaining(name),
            with_effect=inc_stage_retry(name),
        ))
        rules.append(Rule(
            from_state=phase, on_event=Reject, transition_to="failed",
            when=stage_retries_exhausted(name) & recovery_budget_exhausted(),
            with_effect=exhaust_recovery_budget,
        ))
        rules.append(Rule(
            from_state=phase, on_event=Reject, transition_to=recovering_stage,
            when=stage_retries_exhausted(name) & recovery_budget_available(),
            with_effect=enter_recovery,
        ))
    return rules


def list_transitions() -> list[Rule]:
    """Return the default rule table from ``rules.py``."""
    from .rules import RULES
    return list(RULES)
