from dataclasses import dataclass
from typing import Any, Callable

from litehive.lifecycle.events import Blocked, Crash, Event, MergeConflictDetected, Reject
from litehive.lifecycle.persistence import HookRejectFingerprint, LastRejection, TaskState
from litehive.lifecycle.types import FailedReason, NodeName

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
    set_consecutive_same_hook_rejects: int | None = None
    set_last_hook_reject_fingerprint: HookRejectFingerprint | None = None
    clear_hook_reject_tracking: bool = False
    set_hook_reject_recovery_invoked: bool | None = None
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
    source = event.source if isinstance(event, Reject) else None
    reason_code = None
    if isinstance(event, Reject):
        reason = event.reason
        if _hook_reject_loop_detected(state, event):
            reason_code = "hook_reject_loop"
    elif isinstance(event, Crash):
        reason = event.message
    elif isinstance(event, Blocked):
        reason = event.reason
    else:
        reason = None
    context = {
        "trigger_event": type(event).__name__,
        "source": source,
        "reason": reason,
        "raised_at_phase": state.stage,
    }
    hook = _hook_fingerprint_from_event(event)
    if hook is not None:
        context["hook"] = {
            "point": hook.point,
            "command": hook.command,
            "description": hook.description,
            "fingerprint": hook.fingerprint,
        }
    if reason_code is not None:
        context["reason_code"] = reason_code
    return context


def _hook_fingerprint_from_event(event: Event) -> HookRejectFingerprint | None:
    if not isinstance(event, Reject) or event.source != "hook":
        return None
    hook = event.metadata.get("hook")
    if not isinstance(hook, dict):
        return None
    point = hook.get("point")
    command = hook.get("command")
    fingerprint = hook.get("fingerprint")
    if not point or not command or not fingerprint:
        return None
    return HookRejectFingerprint(
        point=point,
        command=command,
        description=hook.get("description", "") or "",
        fingerprint=fingerprint,
    )


def _hook_reject_loop_detected(state: TaskState, event: Event) -> bool:
    if not isinstance(event, Reject) or event.source != "hook":
        return False
    count = event.metadata.get("consecutive_same_hook_rejects")
    return isinstance(count, int) and count >= state.limits.same_hook_reject_limit


def _hook_reject_delta(state: TaskState, event: Event, *, recovery_invoked: bool | None = None) -> StateDelta:
    fingerprint = _hook_fingerprint_from_event(event)
    if fingerprint is None:
        return StateDelta(
            clear_hook_reject_tracking=True,
            set_hook_reject_recovery_invoked=False if recovery_invoked is None else recovery_invoked,
        )
    same_as_last = (
        state.last_hook_reject_fingerprint is not None
        and state.last_hook_reject_fingerprint.fingerprint == fingerprint.fingerprint
    )
    count = state.consecutive_same_hook_rejects + 1 if same_as_last else 1
    return StateDelta(
        set_consecutive_same_hook_rejects=count,
        set_last_hook_reject_fingerprint=fingerprint,
        set_hook_reject_recovery_invoked=(
            recovery_invoked if recovery_invoked is not None else state.hook_reject_recovery_invoked
        ),
    )


def enter_recovery(state: TaskState, event: Event) -> StateDelta:
    hook_delta = _hook_reject_delta(
        state,
        event,
        recovery_invoked=True if _hook_reject_loop_detected(state, event) else None,
    )
    return StateDelta(
        set_origin_stage=state.stage,
        inc_recovery_attempt=state.stage,
        set_consecutive_same_hook_rejects=hook_delta.set_consecutive_same_hook_rejects,
        set_last_hook_reject_fingerprint=hook_delta.set_last_hook_reject_fingerprint,
        clear_hook_reject_tracking=hook_delta.clear_hook_reject_tracking,
        set_hook_reject_recovery_invoked=hook_delta.set_hook_reject_recovery_invoked,
        set_failure_context=_failure_context_from_event(state, event),
    )


def enter_pre_exec_recovery(state: TaskState, event: Event) -> StateDelta:
    return StateDelta(inc_pre_exec_recovery_attempt=True)


def clear_recovery_attempt(state: TaskState, event: Event) -> StateDelta:
    return StateDelta(
        clear_origin_stage=True,
        reset_stage_retry=state.origin_stage,
        clear_hook_reject_tracking=True,
        set_hook_reject_recovery_invoked=False,
    )


def inc_stage_retry(stage: NodeName) -> EffectFn:
    """Effect for reject-retry rules.

    Bumps the stage's retry counter AND captures the rejection so the next
    agent visit can surface it in its prompt.
    """

    def _effect(state: TaskState, event: Event) -> StateDelta:
        rejection = _rejection_from_event(state, event)
        set_rej = (stage, rejection) if rejection is not None else None
        hook_delta = _hook_reject_delta(state, event, recovery_invoked=False)
        return StateDelta(
            inc_stage_retry=stage,
            set_last_rejection=set_rej,
            set_consecutive_same_hook_rejects=hook_delta.set_consecutive_same_hook_rejects,
            set_last_hook_reject_fingerprint=hook_delta.set_last_hook_reject_fingerprint,
            clear_hook_reject_tracking=hook_delta.clear_hook_reject_tracking,
            set_hook_reject_recovery_invoked=hook_delta.set_hook_reject_recovery_invoked,
        )

    return _effect


def stash_conflict_files(state: TaskState, event: Event) -> StateDelta:
    """Effect for ``commit → merge_resolving``.

    Copies the conflict file list from the ``MergeConflictDetected`` event
    into ``state.failure_context`` so the ``MergeAgent`` can read it from
    its prompt context.
    """
    if not isinstance(event, MergeConflictDetected):
        return StateDelta()
    ctx = {
        **state.failure_context,
        "conflict_files": list(event.conflict_files),
        "merge_attempt": state.failure_context.get("merge_attempt", 0) + 1,
    }
    return StateDelta(set_failure_context=ctx)


def fail(reason: FailedReason) -> EffectFn:
    def _effect(state: TaskState, event: Event) -> StateDelta:
        if isinstance(event, (Reject, Blocked)):
            message = event.reason
        elif isinstance(event, Crash):
            message = event.message
        else:
            message = ""
        return StateDelta(failed_reason=reason, failed_message=message)

    return _effect
