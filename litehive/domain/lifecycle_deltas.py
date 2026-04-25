from dataclasses import dataclass
from typing import Callable

from litehive.domain.common import utcnow
from litehive.domain.recovery import (
    FailureFingerprint,
    RecoveryDisposition,
    RecoveryOutcome,
    RecoveryTrigger,
    TriggerEventKind,
    parse_blocked_on_follow_up_reason,
)
from litehive.lifecycle.events import (
    Blocked,
    Crash,
    Event,
    MergeConflictDetected,
    OverallRetryLimitHit,
    RecoveryBudgetHit,
    RecoveryFailed,
    RecoverySucceeded,
    Reject,
    StageRetryLimitHit,
    Timeout,
)
from litehive.lifecycle.persistence import (
    FailedRunRecord,
    HookRejectFingerprint,
    LastRejection,
    MergeContext,
    RejectionLoop,
    TaskState,
)
from litehive.lifecycle.types import FailedReason, NodeName

EffectFn = Callable[[TaskState, Event], "StateDelta"]


@dataclass(frozen=True)
class StateDelta:
    """Typed patch applied by the Runner after a transition fires.

    Every field is optional; only the ones set are applied. No strings to
    parse, no silent drops on typos.
    """

    inc_stage_retry: NodeName | None = None
    reset_stage_retry: NodeName | None = None
    set_active_recovery_trigger: RecoveryTrigger | None = None
    clear_active_recovery_trigger: bool = False
    append_recovery_outcome: RecoveryOutcome | None = None
    inc_pre_exec_recovery_attempt: bool = False
    set_merge_context: MergeContext | None = None
    clear_merge_context: bool = False
    set_last_rejection: tuple[NodeName, LastRejection] | None = None
    set_rejection_loop: RejectionLoop | None = None
    clear_rejection_loop: bool = False
    set_consecutive_same_hook_rejects: int | None = None
    set_last_hook_reject_fingerprint: HookRejectFingerprint | None = None
    clear_hook_reject_tracking: bool = False
    set_hook_reject_recovery_invoked: bool | None = None
    failed_reason: FailedReason | None = None
    failed_message: str | None = None
    set_recovery_failure_explanation: str | None = None
    clear_recovery_failure_explanation: bool = False
    record_failed_run: FailedRunRecord | None = None


EMPTY_DELTA = StateDelta()


def _rejection_from_event(state: TaskState, event: Event) -> LastRejection | None:
    if not isinstance(event, Reject):
        return None
    return LastRejection(
        source=event.source,
        reason=event.reason,
        raised_at_phase=state.stage,
        classification=event.classification,
    )


def _normalized_failure_text(value: str | None) -> str:
    text = " ".join(str(value or "").lower().split())
    return text[:160] or "unknown"


def _event_failure_shape(event: Event) -> str:
    if isinstance(event, Reject):
        hook = _hook_fingerprint_from_event(event)
        if hook is not None:
            return f"hook:{_normalized_failure_text(hook.fingerprint)}"
        classification = event.classification or event.metadata.get("verdict_classification")
        reason_code = event.metadata.get("reason_code")
        if isinstance(classification, str) and classification.strip():
            return f"{event.source}:{_normalized_failure_text(classification)}"
        if isinstance(reason_code, str) and reason_code.strip():
            return f"{event.source}:{_normalized_failure_text(reason_code)}"
        return f"{event.source}:{_normalized_failure_text(event.reason)}"
    if isinstance(event, StageRetryLimitHit):
        return "system:stage_retry_limit"
    return _normalized_failure_text(type(event).__name__)


def _stage_retry_exhausted_record(
    state: TaskState,
    event: Event,
    *,
    failed_reason: FailedReason,
    message: str,
) -> FailedRunRecord | None:
    if failed_reason != FailedReason.SEMANTIC_REJECT:
        return None
    counter_stage = _retry_counter_stage(state.stage)
    if counter_stage is None:
        return None
    if state.stage_retry.get(counter_stage, 0) < state.limits.stage_retry_limit:
        return None
    failure_shape = _event_failure_shape(event)
    source = event.source if isinstance(event, Reject) else None
    classification = event.classification if isinstance(event, Reject) else None
    now = utcnow()
    return FailedRunRecord(
        stage=counter_stage,
        failure_shape=failure_shape,
        count=1,
        first_at=now,
        latest_at=now,
        last_reason=message,
        source=source,
        classification=classification,
        retry_limit=state.limits.stage_retry_limit,
        failed_reason=failed_reason.value,
    )


def _reason_code_from_event(state: TaskState, event: Event) -> str | None:
    if isinstance(event, Reject) and _hook_reject_loop_detected(state, event):
        return "hook_reject_loop"
    if isinstance(event, Reject):
        reason_code = event.metadata.get("reason_code") or event.classification
        if isinstance(reason_code, str) and reason_code.strip():
            return reason_code.strip()
    return None


def _trigger_event_kind(event: Event) -> TriggerEventKind:
    if isinstance(event, Reject):
        return TriggerEventKind.REJECT
    if isinstance(event, Blocked):
        return TriggerEventKind.BLOCKED
    if isinstance(event, Crash):
        return TriggerEventKind.CRASH
    if isinstance(event, Timeout):
        return TriggerEventKind.TIMEOUT
    if isinstance(event, StageRetryLimitHit):
        return TriggerEventKind.STAGE_RETRY_LIMIT
    if isinstance(event, OverallRetryLimitHit):
        return TriggerEventKind.RETRY_LIMIT
    return TriggerEventKind.UNKNOWN


def _fingerprint_from_event(state: TaskState, event: Event) -> FailureFingerprint:
    hook = _hook_fingerprint_from_event(event)
    if hook is not None:
        return FailureFingerprint(
            fingerprint=hook.fingerprint,
            classification="hook_reject",
            diagnostics={
                "point": hook.point,
                "command": hook.command,
                "description": hook.description,
            },
        )
    if isinstance(event, Reject):
        reason_code = _reason_code_from_event(state, event)
        return FailureFingerprint(
            fingerprint=f"{event.source}:{event.reason}",
            classification=event.classification or reason_code or f"{event.source}_reject",
            diagnostics={"source": event.source},
        )
    if isinstance(event, Crash):
        return FailureFingerprint(
            fingerprint=f"{event.exc_type}:{event.message}",
            classification=event.exc_type,
            diagnostics={"exc_type": event.exc_type},
        )
    if isinstance(event, Blocked):
        return FailureFingerprint(
            fingerprint=f"blocked:{event.reason}",
            classification="blocked",
        )
    if isinstance(event, Timeout):
        return FailureFingerprint(
            fingerprint="timeout",
            classification="timeout",
        )
    if isinstance(event, StageRetryLimitHit):
        return FailureFingerprint(
            fingerprint=f"stage_retry_limit:{event.stage}",
            classification="stage_retry_limit",
            diagnostics={"stage": event.stage},
        )
    if isinstance(event, OverallRetryLimitHit):
        return FailureFingerprint(
            fingerprint="retry_limit",
            classification="retry_limit",
        )
    return FailureFingerprint(fingerprint=type(event).__name__.lower())


def recovery_trigger_from_event(state: TaskState, event: Event) -> RecoveryTrigger:
    reason_code = _reason_code_from_event(state, event)
    if isinstance(event, Reject):
        message = event.reason
        source = event.source
        diagnostics = dict(event.metadata or {})
    elif isinstance(event, Crash):
        message = event.message
        source = None
        diagnostics = {"exc_type": event.exc_type}
    elif isinstance(event, Blocked):
        message = event.reason
        source = None
        diagnostics = {}
    elif isinstance(event, StageRetryLimitHit):
        message = f"Stage retry limit exhausted for {event.stage}"
        source = None
        diagnostics = {"stage": event.stage}
    elif isinstance(event, OverallRetryLimitHit):
        message = "Overall retry limit exhausted"
        source = None
        diagnostics = {}
    else:
        message = ""
        source = None
        diagnostics = {}
    return RecoveryTrigger(
        origin_stage=state.stage,
        trigger_event_kind=_trigger_event_kind(event),
        failure_fingerprint=_fingerprint_from_event(state, event),
        source=source,
        reason_code=reason_code,
        message=message,
        diagnostics=diagnostics,
    )


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


def _next_rejection_loop(state: TaskState, event: Event, *, retry_target_stage: NodeName | None) -> RejectionLoop | None:
    if not isinstance(event, Reject) or event.source != "agent":
        return None
    rejection_stage = _pipeline_stage_key(state.stage)
    target_stage = _pipeline_stage_key(retry_target_stage)
    if rejection_stage not in {"testing", "accepting"} or target_stage != "implementing":
        return None
    if state.rejection_loop is None:
        return RejectionLoop(
            rejection_stage=rejection_stage,
            retry_target_stage=target_stage,
            count=1,
        )
    if (
        state.rejection_loop.rejection_stage == rejection_stage
        and state.rejection_loop.retry_target_stage == target_stage
    ):
        count = state.rejection_loop.count + 1
    else:
        count = 1
    return RejectionLoop(
        rejection_stage=rejection_stage,
        retry_target_stage=target_stage,
        count=count,
    )


def rejection_loop_detected(state: TaskState, event: Event, *, retry_target_stage: NodeName | None) -> bool:
    loop = _next_rejection_loop(state, event, retry_target_stage=retry_target_stage)
    return loop is not None and loop.count >= state.limits.rejection_loop_limit


def _rejection_loop_delta(state: TaskState, event: Event, *, retry_target_stage: NodeName | None) -> StateDelta:
    loop = _next_rejection_loop(state, event, retry_target_stage=retry_target_stage)
    if loop is None:
        return StateDelta(clear_rejection_loop=True)
    return StateDelta(set_rejection_loop=loop)


def enter_recovery(state: TaskState, event: Event) -> StateDelta:
    trigger = recovery_trigger_from_event(state, event)
    hook_delta = _hook_reject_delta(
        state,
        event,
        recovery_invoked=True if _hook_reject_loop_detected(state, event) else None,
    )
    return StateDelta(
        set_active_recovery_trigger=trigger,
        set_consecutive_same_hook_rejects=hook_delta.set_consecutive_same_hook_rejects,
        set_last_hook_reject_fingerprint=hook_delta.set_last_hook_reject_fingerprint,
        clear_hook_reject_tracking=hook_delta.clear_hook_reject_tracking,
        set_hook_reject_recovery_invoked=hook_delta.set_hook_reject_recovery_invoked,
        clear_rejection_loop=True,
        clear_recovery_failure_explanation=True,
    )


def enter_pre_exec_recovery(state: TaskState, event: Event) -> StateDelta:
    return StateDelta(inc_pre_exec_recovery_attempt=True)


def _pipeline_stage_key(name: str | None) -> str | None:
    if name in {None, ""}:
        return None
    if name in {"before_grooming", "grooming", "after_grooming", "recovering"}:
        return "grooming"
    if name in {"before_implementing", "implementing", "after_implementing"}:
        return "implementing"
    if name in {"before_testing", "testing", "after_testing"}:
        return "testing"
    if name in {"before_accepting", "accepting", "after_accepting"}:
        return "accepting"
    if name in {"commit", "after_commit", "merge_resolving"}:
        return "commit_to_git"
    return name


def _retry_counter_stage(origin_stage: str | None) -> NodeName | None:
    key = _pipeline_stage_key(origin_stage)
    if key in {"grooming", "implementing", "testing", "accepting", "commit_to_git"}:
        return key
    return origin_stage


def _hook_recovery_made_progress(trigger: RecoveryTrigger | None, event: Event) -> bool:
    if trigger is None or trigger.reason_code != "hook_reject_loop" or not isinstance(event, RecoverySucceeded):
        return False
    target_stage = _pipeline_stage_key(event.resume)
    origin_stage = _pipeline_stage_key(trigger.origin_stage)
    return event.resume == "done" or (target_stage is not None and target_stage != origin_stage)


def clear_recovery_attempt(state: TaskState, event: Event) -> StateDelta:
    trigger = state.active_recovery_trigger
    outcome = None
    if isinstance(event, RecoverySucceeded) and trigger is not None:
        disposition = {
            "resume": RecoveryDisposition.RESUMED,
            "advance": RecoveryDisposition.ADVANCED,
            "done": RecoveryDisposition.COMPLETED,
        }[event.disposition_hint]
        outcome = RecoveryOutcome(
            trigger=trigger,
            recovery_verdict=event.disposition_hint,
            disposition=disposition,
            message=f"Recovery {event.disposition_hint}d task via {event.resume}",
        )
    preserve_hook_tracking = trigger is not None and trigger.reason_code == "hook_reject_loop"
    if preserve_hook_tracking and _hook_recovery_made_progress(trigger, event):
        preserve_hook_tracking = False
    return StateDelta(
        reset_stage_retry=_retry_counter_stage(trigger.origin_stage if trigger is not None else None),
        clear_active_recovery_trigger=True,
        append_recovery_outcome=outcome,
        clear_hook_reject_tracking=not preserve_hook_tracking,
        set_hook_reject_recovery_invoked=False if not preserve_hook_tracking else True,
        clear_rejection_loop=True,
        clear_recovery_failure_explanation=True,
    )


def inc_stage_retry(stage: NodeName, *, retry_target_stage: NodeName | None = None) -> EffectFn:
    """Effect for reject-retry rules.

    Bumps the stage's retry counter AND captures the rejection so the next
    agent visit can surface it in its prompt.
    """

    def _effect(state: TaskState, event: Event) -> StateDelta:
        return _rejection_tracking_delta(
            state,
            event,
            stage=stage,
            increment_retry=True,
            retry_target_stage=retry_target_stage,
        )

    return _effect


def remember_rejection(stage: NodeName, *, retry_target_stage: NodeName | None = None) -> EffectFn:
    """Capture a rejection for a downstream prompt without bumping retries."""

    def _effect(state: TaskState, event: Event) -> StateDelta:
        return _rejection_tracking_delta(
            state,
            event,
            stage=stage,
            increment_retry=False,
            retry_target_stage=retry_target_stage,
        )

    return _effect


def _rejection_tracking_delta(
    state: TaskState,
    event: Event,
    *,
    stage: NodeName,
    increment_retry: bool,
    retry_target_stage: NodeName | None = None,
) -> StateDelta:
    rejection = _rejection_from_event(state, event)
    set_rej = (stage, rejection) if rejection is not None else None
    hook_delta = _hook_reject_delta(state, event, recovery_invoked=False)
    rejection_loop_delta = _rejection_loop_delta(state, event, retry_target_stage=retry_target_stage)
    return StateDelta(
        inc_stage_retry=stage if increment_retry else None,
        set_last_rejection=set_rej,
        set_rejection_loop=rejection_loop_delta.set_rejection_loop,
        clear_rejection_loop=rejection_loop_delta.clear_rejection_loop,
        set_consecutive_same_hook_rejects=hook_delta.set_consecutive_same_hook_rejects,
        set_last_hook_reject_fingerprint=hook_delta.set_last_hook_reject_fingerprint,
        clear_hook_reject_tracking=hook_delta.clear_hook_reject_tracking,
        set_hook_reject_recovery_invoked=hook_delta.set_hook_reject_recovery_invoked,
    )


def fail_rejection_loop(stage: NodeName, *, retry_target_stage: NodeName) -> EffectFn:
    def _effect(state: TaskState, event: Event) -> StateDelta:
        rejection = _rejection_from_event(state, event)
        set_rej = (stage, rejection) if rejection is not None else None
        rejection_loop_delta = _rejection_loop_delta(state, event, retry_target_stage=retry_target_stage)
        message = event.reason if isinstance(event, Reject) else ""
        return StateDelta(
            set_last_rejection=set_rej,
            set_rejection_loop=rejection_loop_delta.set_rejection_loop,
            clear_rejection_loop=rejection_loop_delta.clear_rejection_loop,
            failed_reason=FailedReason.REJECTION_LOOP_DETECTED,
            failed_message=message,
        )

    return _effect


def clear_completed_rejection_loop(state: TaskState, event: Event) -> StateDelta:
    del event
    if state.rejection_loop is None:
        return EMPTY_DELTA
    current_stage = _pipeline_stage_key(state.stage)
    if current_stage != state.rejection_loop.rejection_stage:
        return EMPTY_DELTA
    return StateDelta(clear_rejection_loop=True)


def stash_conflict_files(state: TaskState, event: Event) -> StateDelta:
    """Effect for ``commit → merge_resolving``.

    Copies the conflict file list from the ``MergeConflictDetected`` event
    into ``state.merge_context`` so the ``MergeAgent`` can read it from
    its prompt context.
    """
    if not isinstance(event, MergeConflictDetected):
        return StateDelta()
    current_attempt = state.merge_context.merge_attempt if state.merge_context is not None else 0
    return StateDelta(
        set_merge_context=MergeContext(
            conflict_files=tuple(event.conflict_files),
            merge_attempt=current_attempt + 1,
        )
    )


def fail(reason: FailedReason) -> EffectFn:
    normalized_reason = reason if isinstance(reason, FailedReason) else FailedReason(reason)

    def _effect(state: TaskState, event: Event) -> StateDelta:
        rejection = _rejection_from_event(state, event)
        set_rej = (state.stage, rejection) if rejection is not None else None
        hook_delta = _hook_reject_delta(state, event) if isinstance(event, Reject) else EMPTY_DELTA
        if isinstance(event, (Reject, Blocked)):
            message = event.reason
        elif isinstance(event, Crash):
            message = event.message
        elif isinstance(event, RecoveryFailed):
            message = event.reason
        elif isinstance(event, StageRetryLimitHit):
            message = f"Stage retry limit exhausted for {event.stage}"
        elif isinstance(event, OverallRetryLimitHit):
            message = "Overall retry limit exhausted"
        else:
            message = ""
        outcome = None
        explanation = None
        if state.stage == "recovering":
            trigger = state.active_recovery_trigger
            if trigger is not None:
                outcome = RecoveryOutcome(
                    trigger=trigger,
                    recovery_verdict=_recovery_verdict_for_terminal_event(event, normalized_reason),
                    disposition=RecoveryDisposition.TERMINATED,
                    reason_code=normalized_reason.value,
                    message=message,
                )
                explanation = _recovery_failure_explanation(trigger, normalized_reason, message)
        return StateDelta(
            set_last_rejection=set_rej,
            set_consecutive_same_hook_rejects=hook_delta.set_consecutive_same_hook_rejects,
            set_last_hook_reject_fingerprint=hook_delta.set_last_hook_reject_fingerprint,
            clear_hook_reject_tracking=hook_delta.clear_hook_reject_tracking,
            set_hook_reject_recovery_invoked=hook_delta.set_hook_reject_recovery_invoked,
            failed_reason=normalized_reason,
            failed_message=message,
            record_failed_run=_stage_retry_exhausted_record(
                state,
                event,
                failed_reason=normalized_reason,
                message=message,
            ),
            append_recovery_outcome=outcome,
            clear_active_recovery_trigger=state.stage == "recovering",
            clear_rejection_loop=True,
            set_recovery_failure_explanation=explanation,
        )

    return _effect


def exhaust_recovery_budget(state: TaskState, event: Event) -> StateDelta:
    trigger = recovery_trigger_from_event(state, event)
    return StateDelta(
        failed_reason=FailedReason.RECOVERY_BUDGET_HIT,
        failed_message=trigger.message,
        append_recovery_outcome=RecoveryOutcome(
            trigger=trigger,
            recovery_verdict="budget_hit",
            disposition=RecoveryDisposition.TERMINATED,
            reason_code=FailedReason.RECOVERY_BUDGET_HIT.value,
            message=trigger.message,
        ),
        set_recovery_failure_explanation=_recovery_failure_explanation(
            trigger,
            FailedReason.RECOVERY_BUDGET_HIT,
            trigger.message,
        ),
        clear_rejection_loop=True,
    )


def _recovery_verdict_for_terminal_event(event: Event, reason: FailedReason) -> str:
    if isinstance(event, RecoveryFailed):
        return "failed"
    if isinstance(event, RecoveryBudgetHit):
        return "budget_hit"
    if isinstance(event, Timeout):
        return "timeout"
    if isinstance(event, Crash):
        return "crash"
    return reason.value


def _recovery_failure_explanation(
    trigger: RecoveryTrigger,
    reason: FailedReason,
    message: str,
) -> str:
    subject = trigger.origin_stage or "unknown stage"
    blocked_follow_up_task_id = parse_blocked_on_follow_up_reason(message)
    if blocked_follow_up_task_id is not None:
        return (
            f"Recovery attributed `{subject}` to unrelated breakage and blocked the current task on follow-up "
            f"`{blocked_follow_up_task_id}`."
        )
    if reason == FailedReason.RECOVERY_BUDGET_HIT:
        return (
            f"Recovery budget exhausted for `{subject}` after repeated "
            f"`{trigger.trigger_event_kind.value}` failures with fingerprint "
            f"`{trigger.failure_fingerprint.budget_key()}`."
        )
    if reason == FailedReason.RECOVERY_CRASHED:
        suffix = f": {message}" if message else "."
        return f"Recovery agent crashed while handling `{subject}`{suffix}"
    suffix = f": {message}" if message else "."
    return f"Recovery could not restore a runnable path for `{subject}`{suffix}"
