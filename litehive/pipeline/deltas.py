from dataclasses import dataclass
from typing import Any, Callable

from .events import Event, Reject
from .persistence import LastRejection, TaskState
from .types import FailedReason, NodeName

EffectFn = Callable[[TaskState, Event], "StateDelta"]


@dataclass(frozen=True)
class StateDelta:
    """Typed patch applied by the Runner after a transition fires.

    Every field is optional; only the ones set are applied. No strings to
    parse, no silent drops on typos.
    """

    set_origin_stage: NodeName | None = None
    clear_origin_stage: bool = False
    inc_stage_retry: NodeName | None = None
    reset_stage_retry: NodeName | None = None
    inc_recovery_attempt: NodeName | None = None
    inc_pre_exec_recovery_attempt: bool = False
    set_last_rejection: tuple[NodeName, LastRejection] | None = None
    set_failure_context: dict[str, Any] | None = None
    failed_reason: FailedReason | None = None
    failed_message: str | None = None


EMPTY_DELTA = StateDelta()


def _rejection_from_event(state: TaskState, event: Event) -> LastRejection | None:
    if not isinstance(event, Reject):
        return None
    return LastRejection(
        source=event.source,
        reason=event.reason,
        raised_at_phase=state.stage,
    )


def _failure_context_from_event(state: TaskState, event: Event) -> dict[str, Any]:
    return {
        "trigger_event": type(event).__name__,
        "source": getattr(event, "source", None),
        "reason": getattr(event, "reason", None) or getattr(event, "message", None),
        "raised_at_phase": state.stage,
    }


def enter_recovery(state: TaskState, event: Event) -> StateDelta:
    return StateDelta(
        set_origin_stage=state.stage,
        inc_recovery_attempt=state.stage,
        set_failure_context=_failure_context_from_event(state, event),
    )


def enter_pre_exec_recovery(state: TaskState, event: Event) -> StateDelta:
    return StateDelta(inc_pre_exec_recovery_attempt=True)


def clear_recovery_attempt(state: TaskState, event: Event) -> StateDelta:
    return StateDelta(
        clear_origin_stage=True,
        reset_stage_retry=state.origin_stage,
    )


def inc_stage_retry(stage: NodeName) -> EffectFn:
    """Effect for reject-retry rules.

    Bumps the stage's retry counter AND captures the rejection so the next
    agent visit can surface it in its prompt.
    """

    def _effect(state: TaskState, event: Event) -> StateDelta:
        rejection = _rejection_from_event(state, event)
        set_rej = (stage, rejection) if rejection is not None else None
        return StateDelta(inc_stage_retry=stage, set_last_rejection=set_rej)

    return _effect


def fail(reason: FailedReason) -> EffectFn:
    def _effect(state: TaskState, event: Event) -> StateDelta:
        message = getattr(event, "reason", None) or getattr(event, "message", "")
        return StateDelta(failed_reason=reason, failed_message=message)

    return _effect
