from dataclasses import dataclass
from typing import Callable

from litehive.domain.common import utcnow
from litehive.domain.reports import SEMANTIC_REJECT_CLASSIFICATION
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
    TaskTimeBudgetExceeded,
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
from litehive.domain.common import PipelineState, TaskStage, pipeline_stage_key
from litehive.lifecycle.types import FailedReason

EffectFn = Callable[[TaskState, Event], "StateDelta"]


@dataclass(frozen=True)
class StateDelta:
    """Typed patch applied by the Runner after a transition fires.

    Represents atomic state changes to TaskState that result from pipeline
    events and transitions. Used by the state machine to apply effects
    without risk of incomplete updates or field name typos.

    Created by effect factories based on events, applied by Runner.
    Every field is optional; only the ones set are applied.
    """

    inc_stage_retry: PipelineState | None = None
    reset_stage_retry: PipelineState | None = None
    set_active_recovery_trigger: RecoveryTrigger | None = None
    clear_active_recovery_trigger: bool = False
    append_recovery_outcome: RecoveryOutcome | None = None
    inc_pre_exec_recovery_attempt: bool = False
    set_merge_context: MergeContext | None = None
    clear_merge_context: bool = False
    set_last_rejection: tuple[PipelineState, LastRejection] | None = None
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
    """Lift a `Reject` event into the persisted `LastRejection` shape so
    the next stage prompt can quote the reviewer's verdict; returns `None`
    for non-reject events so callers can pass it through any event."""
    if not isinstance(event, Reject):
        return None
    return LastRejection(
        source=event.source,
        reason=event.reason,
        raised_at_phase=state.stage,
        classification=event.classification,
    )


def _normalized_failure_text(value: str | None) -> str:
    """Squash whitespace, lowercase, and cap a free-text failure description so two equivalent error messages produce the same fingerprint string; used by ``_event_failure_shape`` to keep ``failed_run`` rows clusterable."""
    text = " ".join(str(value or "").lower().split())
    return text[:160] or "unknown"


def _event_failure_shape(event: Event) -> str:
    """Project a failure event onto a stable ``source:detail`` fingerprint that ``_stage_retry_exhausted_record`` writes into ``FailedRunRecord.failure_shape`` so retries that share a cause can be grouped without re-deriving the shape later."""
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
    failed_reason: FailedReason,
    message: str,
) -> FailedRunRecord | None:
    """Emit the ``FailedRunRecord`` written when a stage burns its full retry budget on semantic rejects, so the next run's ``has_blocking_failed_run_history`` check can refuse to requeue a task that will only fail the same way; called by the terminal ``fail`` effect for that one specific reason."""
    if failed_reason != FailedReason.SEMANTIC_REJECT:
        return None
    counter_stage = _retry_counter_stage(state.stage)
    if counter_stage is None:
        return None
    if state.stage_retry.get(counter_stage, 0) < state.limits.stage_retry_limit:
        return None
    failure_shape = _event_failure_shape(event)
    if isinstance(event, Reject):
        source = event.source
    else:
        source = None
    if isinstance(event, Reject):
        classification = event.classification
    else:
        classification = None
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
    """Pull the canonical reason code off a reject so recovery can group
    repeats by cause (e.g. tag a hook-reject loop distinctly from a one-off
    semantic reject). Used both when assembling a recovery trigger and when
    fingerprinting a failure for the budget."""
    if isinstance(event, Reject) and _hook_reject_loop_detected(state, event):
        return "hook_reject_loop"
    if isinstance(event, Reject):
        reason_code = event.metadata.get("reason_code") or event.classification
        if isinstance(reason_code, str) and reason_code.strip():
            return reason_code.strip()
    return None


def _trigger_event_kind(event: Event) -> TriggerEventKind:
    """Map a failure ``Event`` onto the small ``TriggerEventKind`` enum the recovery agent prompt branches on; ``recovery_trigger_from_event`` uses this so the agent sees ``REJECT`` vs ``SEMANTIC_REJECT`` vs ``CRASH`` rather than a Python class name."""
    if isinstance(event, Reject):
        if event.classification == SEMANTIC_REJECT_CLASSIFICATION:
            return TriggerEventKind.SEMANTIC_REJECT
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
    """Build the ``FailureFingerprint`` used to key the per-trigger recovery budget so the same fingerprint cannot loop forever; called by ``recovery_trigger_from_event`` whenever a failure event is being lifted into a ``RecoveryTrigger``."""
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
    """Package a failure event into the `RecoveryTrigger` value object the
    recovery agent reads. Called both by the `recovery_budget_available`
    guard (to look up budget under the right fingerprint) and by the
    recovery factory when launching the recovery agent."""
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
    """Extract the hook identity (point/command/fingerprint) from a hook
    reject's metadata, or `None` if the event is not a fully-formed hook
    reject. Lets downstream code tell "same hook rejected again" from
    "different hook now failing" without re-parsing metadata."""
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
    """True when the same runner hook has rejected the agent enough times
    in a row to count as a loop the recovery agent should break out of.
    The threshold lives on `state.limits.same_hook_reject_limit`."""
    if not isinstance(event, Reject) or event.source != "hook":
        return False
    count = event.metadata.get("consecutive_same_hook_rejects")
    return isinstance(count, int) and count >= state.limits.same_hook_reject_limit


def _hook_reject_delta(state: TaskState, event: Event, recovery_invoked: bool | None = None) -> StateDelta:
    """Compute the slice of state that tracks "is the same hook rejecting
    over and over". Bumps the consecutive-same-hook counter when the
    fingerprint matches, resets it otherwise, and clears tracking when the
    event is not a hook reject. Folded into rejection-tracking, recovery
    entry, and terminal-fail deltas so all three keep the counter coherent."""
    fingerprint = _hook_fingerprint_from_event(event)
    if fingerprint is None:
        if recovery_invoked is None:
            recovery_invoked_value = False
        else:
            recovery_invoked_value = recovery_invoked
        return StateDelta(
            clear_hook_reject_tracking=True,
            set_hook_reject_recovery_invoked=recovery_invoked_value,
        )
    same_as_last = (
        state.last_hook_reject_fingerprint is not None
        and state.last_hook_reject_fingerprint.fingerprint == fingerprint.fingerprint
    )
    if same_as_last:
        count = state.consecutive_same_hook_rejects + 1
    else:
        count = 1
    if recovery_invoked is not None:
        recovery_invoked_value = recovery_invoked
    else:
        recovery_invoked_value = state.hook_reject_recovery_invoked
    return StateDelta(
        set_consecutive_same_hook_rejects=count,
        set_last_hook_reject_fingerprint=fingerprint,
        set_hook_reject_recovery_invoked=recovery_invoked_value,
    )


def _next_rejection_loop(
    state: TaskState, event: Event, retry_target_stage: PipelineState | None
) -> RejectionLoop | None:
    """Decide what the rejection-loop counter should look like after this
    event. Only counts the testing/accepting → implementing bounce that
    the reviewer cares about; anything else returns `None` so the caller
    can clear tracking. Used by both the loop-detection guard and the
    delta that persists the new count."""
    if not isinstance(event, Reject) or event.source != "agent":
        return None
    rejection_stage = _pipeline_stage_key(state.stage)
    target_stage = _pipeline_stage_key(retry_target_stage)
    if rejection_stage not in {TaskStage.TESTING, TaskStage.ACCEPTING} or target_stage != TaskStage.IMPLEMENTING:
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


def rejection_loop_detected(state: TaskState, event: Event, retry_target_stage: PipelineState | None) -> bool:
    """True when reviewer rejects keep cycling testing/accepting back to
    implementing without progress. Backs the `rejection_loop_detected`
    guard in `retry_epoch_rules`, which fails the task instead of looping
    forever on the same retry target."""
    loop = _next_rejection_loop(state, event, retry_target_stage=retry_target_stage)
    return loop is not None and loop.count >= state.limits.rejection_loop_limit


def _rejection_loop_delta(state: TaskState, event: Event, retry_target_stage: PipelineState | None) -> StateDelta:
    """State patch that either advances or clears the rejection-loop
    counter for this event. Folded into the retry/remember effects and the
    terminal `FailRejectionLoop` effect so the persisted counter always
    matches what the loop guard saw."""
    loop = _next_rejection_loop(state, event, retry_target_stage=retry_target_stage)
    if loop is None:
        return StateDelta(clear_rejection_loop=True)
    return StateDelta(set_rejection_loop=loop)


def enter_recovery(state: TaskState, event: Event) -> StateDelta:
    """Effect for any rule that escalates a stage failure into the recovery
    agent (the `_recovery_rules(...)` rows in the rule table). Records the
    trigger the recovery agent will read, marks hook-reject tracking as
    "recovery now owns this loop", and clears stale rejection-loop /
    explanation state so the next attempt starts clean."""
    trigger = recovery_trigger_from_event(state, event)
    if _hook_reject_loop_detected(state, event):
        hook_recovery_invoked: bool | None = True
    else:
        hook_recovery_invoked = None
    hook_delta = _hook_reject_delta(
        state,
        event,
        recovery_invoked=hook_recovery_invoked,
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
    """Effect for the rule that fires when a task wakes up already
    needing pre-exec recovery (worktree never reached a clean state).
    Bumps the pre-exec attempt counter so the budget guard can stop
    retrying eventually."""
    del state, event
    return StateDelta(inc_pre_exec_recovery_attempt=True)


def _pipeline_stage_key(name: str | None) -> str | None:
    """Same as ``litehive.domain.common.pipeline_stage_key`` plus
    treating empty string as ``None``."""
    if name == "":
        return None
    return pipeline_stage_key(name)


def _retry_counter_stage(origin_stage: str | None) -> PipelineState | None:
    """Map any stage label (including pre/post phase variants) to the
    canonical pipeline stage that owns its retry counter. Used when
    bumping retries on a rejection and when resetting them after a
    successful recovery, so both sides target the same bucket."""
    key = _pipeline_stage_key(origin_stage)
    if key in {
        TaskStage.GROOMING,
        TaskStage.IMPLEMENTING,
        TaskStage.TESTING,
        TaskStage.ACCEPTING,
        TaskStage.COMMIT_TO_GIT,
    }:
        return key
    return origin_stage


def _hook_recovery_made_progress(trigger: RecoveryTrigger | None, event: Event) -> bool:
    """True when a recovery agent invoked specifically to break a hook-reject loop has returned a resume target that actually moves the task off the looping stage (or to ``done``); ``record_recovery_success`` uses this to decide whether to clear the hook-reject tracking or keep it sticky."""
    if trigger is None or trigger.reason_code != "hook_reject_loop" or not isinstance(event, RecoverySucceeded):
        return False
    target_stage = _pipeline_stage_key(event.resume)
    origin_stage = _pipeline_stage_key(trigger.origin_stage)
    return event.resume == "done" or (target_stage is not None and target_stage != origin_stage)


def record_recovery_success(state: TaskState, event: Event) -> StateDelta:
    """Effect for the rule that fires when the recovery agent reports
    success and chooses a concrete resume stage. Resets the failed
    stage's retry counter (the agent says it fixed it), appends an
    outcome to the recovery history, and only clears hook-reject
    tracking if the resume actually moves past the looping stage."""
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
    if trigger is not None:
        retry_counter_stage_arg = trigger.origin_stage
    else:
        retry_counter_stage_arg = None
    if not preserve_hook_tracking:
        hook_recovery_invoked_value = False
    else:
        hook_recovery_invoked_value = True
    return StateDelta(
        reset_stage_retry=_retry_counter_stage(retry_counter_stage_arg),
        clear_active_recovery_trigger=True,
        append_recovery_outcome=outcome,
        clear_hook_reject_tracking=not preserve_hook_tracking,
        set_hook_reject_recovery_invoked=hook_recovery_invoked_value,
        clear_rejection_loop=True,
        clear_recovery_failure_explanation=True,
    )


@dataclass(frozen=True)
class IncStageRetry:
    """Bump the stage's retry counter AND capture the rejection so the next agent visit can surface it in its prompt."""

    stage: PipelineState
    retry_target_stage: PipelineState | None = None

    def __call__(self, state: TaskState, event: Event) -> StateDelta:
        """Effect entry point invoked by the rule engine; delegates to ``_rejection_tracking_delta`` with ``increment_retry=True`` so the stage retry counter advances alongside the captured rejection."""
        return _rejection_tracking_delta(
            state,
            event,
            stage=self.stage,
            increment_retry=True,
            retry_target_stage=self.retry_target_stage,
        )


@dataclass(frozen=True)
class RememberRejection:
    """Capture a rejection for a downstream prompt without bumping retries."""

    stage: PipelineState
    retry_target_stage: PipelineState | None = None

    def __call__(self, state: TaskState, event: Event) -> StateDelta:
        """Effect entry point invoked by the rule engine; delegates to ``_rejection_tracking_delta`` with ``increment_retry=False`` so the rejection is remembered for the next prompt without burning a retry slot."""
        return _rejection_tracking_delta(
            state,
            event,
            stage=self.stage,
            increment_retry=False,
            retry_target_stage=self.retry_target_stage,
        )


def _rejection_tracking_delta(
    state: TaskState,
    event: Event,
    stage: PipelineState,
    increment_retry: bool,
    retry_target_stage: PipelineState | None = None,
) -> StateDelta:
    """Shared body for the `IncStageRetry` and `RememberRejection`
    effects: capture the reviewer's reject for the next prompt, update
    hook-reject and rejection-loop counters, and optionally bump the
    stage retry counter. The `increment_retry` flag is what tells the
    two factories apart."""
    rejection = _rejection_from_event(state, event)
    if rejection is not None:
        set_rej = (stage, rejection)
    else:
        set_rej = None
    hook_delta = _hook_reject_delta(state, event, recovery_invoked=False)
    rejection_loop_delta = _rejection_loop_delta(state, event, retry_target_stage=retry_target_stage)
    if increment_retry:
        inc_stage_retry_arg = stage
    else:
        inc_stage_retry_arg = None
    return StateDelta(
        inc_stage_retry=inc_stage_retry_arg,
        set_last_rejection=set_rej,
        set_rejection_loop=rejection_loop_delta.set_rejection_loop,
        clear_rejection_loop=rejection_loop_delta.clear_rejection_loop,
        set_consecutive_same_hook_rejects=hook_delta.set_consecutive_same_hook_rejects,
        set_last_hook_reject_fingerprint=hook_delta.set_last_hook_reject_fingerprint,
        clear_hook_reject_tracking=hook_delta.clear_hook_reject_tracking,
        set_hook_reject_recovery_invoked=hook_delta.set_hook_reject_recovery_invoked,
    )


@dataclass(frozen=True)
class FailRejectionLoop:
    """Mark the task failed when reviewer rejects keep bouncing it back without progress; wired in by ``retry_epoch_rules`` for testing/accepting."""

    stage: PipelineState
    retry_target_stage: PipelineState

    def __call__(self, state: TaskState, event: Event) -> StateDelta:
        """Effect entry point invoked by the rule engine when ``rejection_loop_detected`` fires; remembers the final rejection on the looping stage and fails the task with ``REJECTION_LOOP_DETECTED`` so it stops bouncing."""
        rejection = _rejection_from_event(state, event)
        if rejection is not None:
            set_rej = (self.stage, rejection)
        else:
            set_rej = None
        rejection_loop_delta = _rejection_loop_delta(
            state, event, retry_target_stage=self.retry_target_stage
        )
        if isinstance(event, Reject):
            message = event.reason
        else:
            message = ""
        return StateDelta(
            set_last_rejection=set_rej,
            set_rejection_loop=rejection_loop_delta.set_rejection_loop,
            clear_rejection_loop=rejection_loop_delta.clear_rejection_loop,
            failed_reason=FailedReason.REJECTION_LOOP_DETECTED,
            failed_message=message,
        )


def clear_completed_rejection_loop(state: TaskState, event: Event) -> StateDelta:
    """Effect for the rule that fires when a stage passes after a prior
    reject loop targeted it. Wipes the rejection-loop counter so the next
    failure starts fresh; attached to every stage's `Pass` rule
    (grooming/implementing/testing/accepting) in the rule table."""
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
    if state.merge_context is not None:
        current_attempt = state.merge_context.merge_attempt
    else:
        current_attempt = 0
    return StateDelta(
        set_merge_context=MergeContext(
            conflict_files=tuple(event.conflict_files),
            merge_attempt=current_attempt + 1,
        )
    )


@dataclass(frozen=True)
class Fail:
    """Drive the task into ``FAILED`` and record the cause; used for terminal rejects, retry exhaustion, time-budget hits, and recovery-agent failures."""

    reason: FailedReason

    def __post_init__(self) -> None:
        """Coerce a string reason passed by older rule rows into the ``FailedReason`` enum so downstream effect application sees a single, validated type even on a frozen dataclass."""
        if not isinstance(self.reason, FailedReason):
            object.__setattr__(self, "reason", FailedReason(self.reason))

    def __call__(self, state: TaskState, event: Event) -> StateDelta:
        """Effect entry point invoked by the rule engine for terminal-failure rows; assembles the failed reason / message, reconstructs hook-reject tracking, and (when failing inside RECOVERING) records the terminated ``RecoveryOutcome`` plus a human-readable explanation."""
        rejection = _rejection_from_event(state, event)
        if rejection is not None:
            set_rej = (state.stage, rejection)
        else:
            set_rej = None
        if isinstance(event, Reject):
            hook_delta = _hook_reject_delta(state, event)
        else:
            hook_delta = EMPTY_DELTA
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
        elif isinstance(event, TaskTimeBudgetExceeded):
            message = (
                f"Task time budget exceeded before commit: "
                f"{event.elapsed_seconds:.1f}s elapsed, budget {event.budget_seconds:.1f}s"
            )
        else:
            message = ""
        outcome = None
        explanation = None
        if state.stage == PipelineState.RECOVERING:
            trigger = state.active_recovery_trigger
            if trigger is not None:
                outcome = RecoveryOutcome(
                    trigger=trigger,
                    recovery_verdict=_recovery_verdict_for_terminal_event(event, self.reason),
                    disposition=RecoveryDisposition.TERMINATED,
                    reason_code=self.reason.value,
                    message=message,
                )
                explanation = _recovery_failure_explanation(trigger, self.reason, message)
        return StateDelta(
            set_last_rejection=set_rej,
            set_consecutive_same_hook_rejects=hook_delta.set_consecutive_same_hook_rejects,
            set_last_hook_reject_fingerprint=hook_delta.set_last_hook_reject_fingerprint,
            clear_hook_reject_tracking=hook_delta.clear_hook_reject_tracking,
            set_hook_reject_recovery_invoked=hook_delta.set_hook_reject_recovery_invoked,
            failed_reason=self.reason,
            failed_message=message,
            record_failed_run=_stage_retry_exhausted_record(
                state,
                event,
                failed_reason=self.reason,
                message=message,
            ),
            append_recovery_outcome=outcome,
            clear_active_recovery_trigger=state.stage == PipelineState.RECOVERING,
            clear_rejection_loop=True,
            set_recovery_failure_explanation=explanation,
        )


def exhaust_recovery_budget(state: TaskState, event: Event) -> StateDelta:
    """Effect for the rule that fires when a stage failure would normally
    trigger recovery but the per-trigger budget for this fingerprint is
    already spent. Fails the task with `recovery_budget_hit`, records the
    terminated outcome, and writes the explanation users see in the
    failure summary."""
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
    """Pick the short verdict label persisted on ``RecoveryOutcome`` when a task dies inside recovering; the terminal ``fail`` effect uses this so timeline views can distinguish "agent failed" from "budget hit" from "crashed inside recovery"."""
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
    """Human-readable sentence shown in the failure summary when a task
    dies inside or because of recovery. Branches on whether recovery
    blocked on a follow-up task, ran out of budget, crashed, or simply
    couldn't restore a runnable path. Called from the terminal `fail`
    effect and from `exhaust_recovery_budget`."""
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
        if message:
            suffix = f": {message}"
        else:
            suffix = "."
        return f"Recovery agent crashed while handling `{subject}`{suffix}"
    if message:
        suffix = f": {message}"
    else:
        suffix = "."
    return f"Recovery could not restore a runnable path for `{subject}`{suffix}"
