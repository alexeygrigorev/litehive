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
    Fail,
    FailRejectionLoop,
    IncStageRetry,
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
from .types import FailedReason, PipelineMode, STAGES

ToFn = Callable[[TaskState, Event], PipelineState]
ToSpec = PipelineState | str | ToFn | Stage


def _entry_phase(stage: str | PipelineState | None) -> PipelineState:
    """
    Translate a logical stage into the phase the runner re-enters on resume.

    Every agent stage has a ``before_<stage>`` pre-hook phase that the
    pipeline runs through first, but COMMIT has no ``before_commit``,
    so it resumes at itself. Used by the recovery and worktree-sync
    callable resume targets in this module.
    """
    if stage is None:
        return PipelineState.READY
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
        """Build a ``NoTransitionError`` capturing the unmatched (state, event) pair for diagnostics.

        Raised by ``evaluate`` when none of the rules accept the current
        state/event combo; the runner reports the captured fields so we
        can spot a missing rule from a stack trace alone.
        """
        super().__init__(f"no transition rule matched: current={current!r} event={type(event).__name__}")
        self.current = current
        self.event = event


def _matches_from(pattern, current: PipelineState) -> bool:
    """Test whether a rule's ``from_state`` pattern accepts ``current``.

    The rule table mixes single states, frozensets, ``Stage`` enums, and
    raw strings; centralizing the comparison here keeps ``evaluate`` short
    and lets new pattern shapes drop in without rewriting the matcher.
    """
    if isinstance(pattern, frozenset):
        return current in pattern
    if isinstance(pattern, Stage):
        return pattern.name == current
    return canonical_pipeline_state(pattern) == current


def _matches_event(pattern: type[Event], event: Event) -> bool:
    """Test whether ``event`` is an instance of the rule's ``on_event`` class.

    Trivial wrapper so the ``evaluate`` loop reads symmetrically with
    ``_matches_from``: every rule has both a state matcher and an event
    matcher, written the same way.
    """
    return isinstance(event, pattern)


def _resolve_to(to: ToSpec, state: TaskState, event: Event) -> PipelineState:
    """Resolve a rule's ``transition_to`` (state, ``Stage``, or callable) to a concrete ``PipelineState``.

    Used by ``evaluate`` so callable destinations (``resume_from_origin``,
    ``entry_from_worktree_sync``, etc.) can decide the next state from
    runtime context while static destinations stay as plain enum members.
    """
    if callable(to) and not isinstance(to, Stage):
        return canonical_pipeline_state(to(state, event))
    if isinstance(to, Stage):
        return to.name
    return canonical_pipeline_state(to)


def evaluate(rules: list[Rule], current: str | PipelineState, event: Event, state: TaskState) -> Transition:
    """
    Find the first rule that matches the current state and event.

    Pure function — no I/O, no mutation. Iterating top-to-bottom and
    returning on the first match is the rule-table contract: the
    order in ``rules.py`` is significant, with more specific rows
    placed before catch-all wildcards.
    """
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
    """
    Pick where the task re-enters after the recovery agent reports success.

    Used as a ``transition_to`` callable from the global RULES table
    when leaving the ``recovering`` node. The destination depends on
    what triggered recovery: an event hint takes precedence, then the
    active trigger's origin stage, and ``"done"`` short-circuits to
    the terminal node when the recovery agent decided no resume is
    needed.
    """
    if not isinstance(event, RecoverySucceeded):
        raise TypeError(f"resume_from_origin expects RecoverySucceeded, got {type(event).__name__}")
    e = event
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
    """
    Pick where to enter the pipeline after pre-exec recovery self-heals.

    Wired into the RULES table as the ``transition_to`` for
    ``PreExecRecoverySucceeded`` so the runner respects whatever phase
    the probe asked us to resume at — typically ``before_grooming`` for
    full mode or ``before_implementing`` for single mode.
    """
    del state
    if not isinstance(event, PreExecRecoverySucceeded):
        raise TypeError(f"resume_from_pre_exec expects PreExecRecoverySucceeded, got {type(event).__name__}")
    e = event
    if e.resume_stage in STAGES:
        return _entry_phase(e.resume_stage)
    return canonical_pipeline_state(e.resume_stage)


def entry_from_worktree_sync(state: TaskState, event: Event) -> PipelineState:
    """
    Pick the first agent phase a freshly-synced worktree should enter.

    Honours an explicit ``entry_stage`` saved on the task (so a resumed
    or single-stage launch lands where the operator asked) and
    otherwise falls back to the pipeline-mode default. Used as the
    ``transition_to`` after the worktree-sync node passes.
    """
    del event
    if state.entry_stage in STAGES:
        return _entry_phase(state.entry_stage)
    if state.entry_stage:
        return canonical_pipeline_state(state.entry_stage)
    if state.pipeline_mode == PipelineMode.SINGLE:
        return PipelineState.BEFORE_IMPLEMENTING
    return PipelineState.BEFORE_GROOMING


# ── rule generators (used by rules.py) ──────────────────────────────────


def retry_epoch_rules(counter_stage, phases, retry_target, exhausted_reason: FailedReason) -> list[Rule]:
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
                transition_to=PipelineState.FAILED,
                when=rejection_loop_detected(retry_target_name),
                with_effect=FailRejectionLoop(
                    name,
                    retry_target_stage=retry_target_name,
                ),
            )
        )
        rules.append(
            Rule(
                from_state=phase,
                on_event=Reject,
                transition_to=PipelineState.FAILED,
                when=hook_reject_loop_detected(),
                with_effect=Fail(FailedReason.HOOK_REJECT_LOOP),
            )
        )
        rules.append(
            Rule(
                from_state=phase,
                on_event=Reject,
                transition_to=retry_target,
                when=stage_retries_remaining(name),
                with_effect=IncStageRetry(
                    name,
                    retry_target_stage=retry_target_name,
                ),
            )
        )
        rules.append(
            Rule(
                from_state=phase,
                on_event=Reject,
                transition_to=PipelineState.FAILED,
                when=stage_retries_exhausted(name),
                with_effect=Fail(exhausted_reason),
            )
        )
    return rules


def list_transitions() -> list[Rule]:
    """Return a fresh copy of the default rule table — used by diagnostics tooling that needs to enumerate transitions without depending on a particular import order."""
    # inline: rules.py top-level-imports transitions.py for ``Rule`` etc.
    from .rules import RULES  # noqa: PLC0415

    return list(RULES)
