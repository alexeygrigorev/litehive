"""
Effect functions and deltas applied by the pipeline state machine.

The state machine in ``litehive.lifecycle`` matches an ``Event`` against
its rule table and runs the matched effect; each effect here returns a
``StateDelta`` the runner applies atomically to ``TaskState``. The
deltas are kept tiny and named so a rule row reads as "on Reject in
ACCEPTING, run ``IncStageRetry`` and ``record_recovery_success`` later"
rather than baking ad-hoc state mutations into rule code.

The other half of this module is the recovery-trigger plumbing:
projecting any failure event into a ``RecoveryTrigger`` keyed by
``FailureFingerprint`` so the recovery budget can group similar
failures and refuse to loop forever on the same cause.
"""

from dataclasses import dataclass
from typing import Callable

from litehive.domain.common import utcnow
from litehive.domain.recovery import (
    FailureFingerprint,
    RecoveryDisposition,
    RecoveryOutcome,
    RecoveryTrigger,
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
    """
    Typed patch applied by the runner after a transition fires.

    Represents the atomic mutation a single effect performs on
    ``TaskState``: each field is optional, and only the ones the effect
    set are applied. This shape exists so effect functions can compose
    cleanly (one delta builds and returns another's fields) and so the
    runner can never partially apply a half-built mutation — the rule
    engine sees the whole patch or none of it.
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
    """
    Lift a ``Reject`` event into the persisted ``LastRejection`` shape.

    The next stage's prompt quotes the previous reviewer verdict from
    this record so the agent sees what it was rejected for. Returns
    ``None`` for non-reject events so the rejection-tracking effects
    below can call this on any event without branching on type first.
    """
    if not isinstance(event, Reject):
        return None
    return LastRejection(
        source=event.source,
        reason=event.reason,
        raised_at_phase=state.stage,
        classification=event.classification,
    )


def _normalized_failure_text(value: str | None) -> str:
    """
    Normalize a free-text failure description to a stable fingerprint string.

    Squashes whitespace, lowercases, and caps at 160 chars so two
    equivalent error messages (modulo capitalization or trailing
    detail) produce the same fingerprint. ``_event_failure_shape``
    uses this to make ``failed_run`` rows clusterable for the
    "this task keeps failing the same way" check.
    """
    text = " ".join(str(value or "").lower().split())
    return text[:160] or "unknown"


def _event_failure_shape(event: Event) -> str:
    """
    Project a failure event onto a stable ``source:detail`` fingerprint.

    Written into ``FailedRunRecord.failure_shape`` by
    ``_stage_retry_exhausted_record`` so retries that share a root
    cause cluster under the same row, letting the rejection-loop and
    blocking-history checks ask "have we already given up on this
    shape?" without re-parsing the original event each time.
    """
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
    """
    Emit the ``FailedRunRecord`` for a stage that exhausted its retry budget.

    Only fires for ``SEMANTIC_REJECT`` exhaustion: those are the
    failures the next run's ``has_blocking_failed_run_history`` check
    refuses to requeue on. Other terminal reasons (crashes,
    user-aborts) go through different paths because retrying them
    cannot fail "the same way". Called only by the terminal ``fail``
    effect.
    """
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
    """
    Pull the canonical reason code off a reject for recovery grouping.

    Distinguishes a hook-reject loop from a one-off semantic reject so
    the recovery budget tracks them separately — burning ten retries on
    "tests failed" should not also exhaust the budget for "the
    pre-commit hook keeps complaining". Used both when assembling a
    ``RecoveryTrigger`` and when fingerprinting the failure for budget
    bookkeeping.
    """
    if isinstance(event, Reject) and _hook_reject_loop_detected(state, event):
        return "hook_reject_loop"
    if isinstance(event, Reject):
        reason_code = event.metadata.get("reason_code") or event.classification
        if isinstance(reason_code, str) and reason_code.strip():
            return reason_code.strip()
    return None


def _fingerprint_from_event(state: TaskState, event: Event) -> FailureFingerprint:
    """
    Build the ``FailureFingerprint`` keying the per-trigger recovery budget.

    The budget caps how many times the same fingerprint may trigger
    recovery, so two unrelated rejects cannot together exhaust the
    budget and one persistent reject cannot loop forever. Called by
    ``recovery_trigger_from_event`` whenever a failure event is being
    lifted into a ``RecoveryTrigger``.
    """
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
    """
    Package a failure event into the recovery agent's ``RecoveryTrigger``.

    Two callers consume this: the ``recovery_budget_available`` guard
    looks up the per-fingerprint budget before deciding whether to
    enter recovery, and the recovery-entry effect persists the trigger
    so the recovery agent can read it from prompt context. Keeping
    both on the same construction means budget and prompt always agree
    about the cause.
    """
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
        trigger_event_kind=event.trigger_event_kind,
        failure_fingerprint=_fingerprint_from_event(state, event),
        source=source,
        reason_code=reason_code,
        message=message,
        diagnostics=diagnostics,
    )


def _hook_fingerprint_from_event(event: Event) -> HookRejectFingerprint | None:
    """
    Extract the hook identity from a hook reject event.

    Returns ``(point, command, description, fingerprint)`` or ``None``
    when the event is not a fully-formed hook reject. Downstream code
    uses this to tell "same hook rejected again" (advance the
    consecutive counter) from "different hook now failing" (reset)
    without re-parsing the event's metadata at every call site.
    """
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
    """
    True when the same hook has rejected the agent enough times to count as a loop.

    Threshold lives on ``state.limits.same_hook_reject_limit`` so it
    can be tuned per workspace. The runner's hook layer increments the
    consecutive counter and embeds it in the event metadata; this
    helper just reads that counter so the lifecycle layer can decide
    whether to escalate to recovery instead of bumping retries forever.
    """
    if not isinstance(event, Reject) or event.source != "hook":
        return False
    count = event.metadata.get("consecutive_same_hook_rejects")
    return isinstance(count, int) and count >= state.limits.same_hook_reject_limit


def _hook_reject_delta(state: TaskState, event: Event, recovery_invoked: bool | None = None) -> StateDelta:
    """
    Compute the state slice tracking "is the same hook rejecting over and over".

    Bumps the consecutive-same-hook counter on a fingerprint match,
    resets to one on a different hook, and clears tracking entirely
    when the event is not a hook reject. Folded into rejection-tracking,
    recovery-entry, and terminal-fail deltas so every path that handles
    a Reject keeps the counter coherent — without that sharing a stage
    transition could miss the bump and the loop guard would never fire.
    """
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
    """
    Decide what the rejection-loop counter should look like after this event.

    Only the testing/accepting → implementing bounce counts as a loop —
    that's the cycle the reviewer is trying to break out of. Any other
    transition returns ``None`` so the caller can clear tracking
    instead of carrying a stale count into a different scenario. Used
    by both the loop-detection guard and the delta that persists the
    new count.
    """
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
    """
    True when reviewer rejects keep cycling without progress.

    Backs the ``rejection_loop_detected`` guard in ``retry_epoch_rules``;
    when it fires the runner fails the task with
    ``REJECTION_LOOP_DETECTED`` instead of looping forever on the same
    retry target. The threshold lives on
    ``state.limits.rejection_loop_limit`` so workspaces can tune it.
    """
    loop = _next_rejection_loop(state, event, retry_target_stage=retry_target_stage)
    return loop is not None and loop.count >= state.limits.rejection_loop_limit


def _rejection_loop_delta(state: TaskState, event: Event, retry_target_stage: PipelineState | None) -> StateDelta:
    """
    State patch that advances or clears the rejection-loop counter.

    Folded into the retry/remember effects and the terminal
    ``FailRejectionLoop`` effect so the persisted counter always
    matches what the ``rejection_loop_detected`` guard saw — a
    mismatch would let the guard fire on a stale count.
    """
    loop = _next_rejection_loop(state, event, retry_target_stage=retry_target_stage)
    if loop is None:
        return StateDelta(clear_rejection_loop=True)
    return StateDelta(set_rejection_loop=loop)


def enter_recovery(state: TaskState, event: Event) -> StateDelta:
    """
    Effect for rules that escalate a stage failure into the recovery agent.

    Wired in by every ``_recovery_rules(...)`` row in the lifecycle
    rule table. Records the ``RecoveryTrigger`` the recovery agent
    will read from prompt context, marks hook-reject tracking as
    "recovery now owns this loop" so a successful recovery can clear
    it, and wipes stale rejection-loop and recovery-explanation fields
    so the next recovery attempt starts on a clean slate.
    """
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
    """
    Effect for the wake-up-into-pre-exec-recovery transition.

    Fires when a task wakes up still needing pre-exec recovery — i.e.
    its worktree never reached a clean state on the previous run.
    Bumps the pre-exec attempt counter so the budget guard can
    eventually stop retrying instead of looping on a workspace that
    will never come clean on its own.
    """
    del state, event
    return StateDelta(inc_pre_exec_recovery_attempt=True)


def _pipeline_stage_key(name: str | None) -> str | None:
    """
    Local wrapper around ``pipeline_stage_key`` that also nulls out empty strings.

    Lifecycle deltas pull stage names off events that may carry empty
    strings (e.g. a follow-up event before a stage is set), and the
    downstream comparisons want a single sentinel ``None`` rather than
    a mix of ``""`` and ``None``. The shared helper does not collapse
    empties because other callers want to round-trip them as-is.
    """
    if name == "":
        return None
    return pipeline_stage_key(name)


def _retry_counter_stage(origin_stage: str | None) -> PipelineState | None:
    """
    Map any stage label to the canonical stage that owns its retry counter.

    Pre/post hook variants (``BEFORE_TESTING``, ``AFTER_TESTING``…)
    share a counter with their owning stage so a hook-rejected attempt
    burns the same retry budget as the agent attempt itself. Used both
    when bumping retries on a rejection and when resetting them after
    a successful recovery — both sides need to target the same bucket
    or the counter and the reset can drift apart.
    """
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
    """
    True when a hook-loop recovery actually moved the task off the looping stage.

    A recovery invoked specifically to break a hook-reject loop has to
    *do something* — resume past the looping stage, advance to a new
    one, or finish the task. If it just resumes back into the same
    stage we keep the hook-reject tracking sticky so the next reject
    on the same fingerprint still fails fast instead of starting the
    loop counter over. Consulted by ``record_recovery_success``.
    """
    if trigger is None or trigger.reason_code != "hook_reject_loop" or not isinstance(event, RecoverySucceeded):
        return False
    target_stage = _pipeline_stage_key(event.resume)
    origin_stage = _pipeline_stage_key(trigger.origin_stage)
    return event.resume == "done" or (target_stage is not None and target_stage != origin_stage)


def record_recovery_success(state: TaskState, event: Event) -> StateDelta:
    """
    Effect that fires when the recovery agent reports success.

    Resets the failed stage's retry counter (the agent says it fixed
    it), appends a ``RecoveryOutcome`` to the history for audit, and
    only clears hook-reject tracking when the resume target actually
    moves past the looping stage — keeping it sticky on a same-stage
    resume protects against a recovery that ends without making real
    progress.
    """
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
    """
    Effect: bump the stage retry counter and capture the rejection.

    Wired in for "retry the same stage" rule rows. The captured
    rejection is what the next agent visit quotes in its prompt — the
    rule cannot just bump retries without also remembering the
    rejection or the agent would re-attempt blindly.
    """

    stage: PipelineState
    retry_target_stage: PipelineState | None = None

    def __call__(self, state: TaskState, event: Event) -> StateDelta:
        """
        Rule-engine entry: delegate to ``_rejection_tracking_delta`` with retry on.

        ``increment_retry=True`` is what distinguishes this effect
        from ``RememberRejection``; both share the rejection-capture
        plumbing, only the retry-counter side flips.
        """
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
        """
        Rule-engine entry: delegate to ``_rejection_tracking_delta`` with retry off.

        ``increment_retry=False`` is what distinguishes this effect
        from ``IncStageRetry``: the rejection is remembered so the
        next stage's prompt still sees it, but no retry slot is
        consumed because this transition is bouncing to a different
        stage rather than re-running the same one.
        """
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
    """
    Shared body for the ``IncStageRetry`` and ``RememberRejection`` effects.

    Captures the reviewer's reject for the next prompt, updates the
    hook-reject and rejection-loop counters, and optionally bumps the
    stage retry counter. The ``increment_retry`` flag is the single
    knob that separates the two effect factories — keeping the rest
    of the bookkeeping shared prevents the two paths from drifting
    out of sync on a refactor.
    """
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
    """
    Effect: fail the task when reviewer rejects keep bouncing it back without progress.

    Wired in by ``retry_epoch_rules`` for the testing and accepting
    stages — those are the ones a stuck reviewer can spin forever.
    Other stages do not need this effect because their retry exhaustion
    already terminates them.
    """

    stage: PipelineState
    retry_target_stage: PipelineState

    def __call__(self, state: TaskState, event: Event) -> StateDelta:
        """
        Rule-engine entry: terminate when ``rejection_loop_detected`` has fired.

        Records the final rejection on the looping stage so the
        operator's failure summary shows the cause, then fails the
        task with ``REJECTION_LOOP_DETECTED`` so it stops bouncing
        between stages and the recovery agent gets a clean handoff.
        """
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
    """
    Effect that wipes the rejection-loop counter when a stage finally passes.

    Attached to every stage's ``Pass`` rule
    (grooming/implementing/testing/accepting) so a future reject on
    the same stage starts a fresh loop count rather than carrying old
    bounces forward. Only clears when the passing stage matches the
    one tracked by the loop, so a Pass on a different stage doesn't
    erase an in-progress loop on the original one.
    """
    del event
    if state.rejection_loop is None:
        return EMPTY_DELTA
    current_stage = _pipeline_stage_key(state.stage)
    if current_stage != state.rejection_loop.rejection_stage:
        return EMPTY_DELTA
    return StateDelta(clear_rejection_loop=True)


def stash_conflict_files(state: TaskState, event: Event) -> StateDelta:
    """
    Effect for the ``commit → merge_resolving`` transition.

    Copies the conflict file list off the ``MergeConflictDetected``
    event and onto ``state.merge_context`` so the ``MergeAgent`` can
    read the unresolved paths from its prompt context. Bumps the
    ``merge_attempt`` counter at the same time so the recovery
    explanation can mention which attempt is in progress.
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
    """
    Effect: drive the task into ``FAILED`` and record the cause.

    Covers every terminal failure path: a final reject, retry
    exhaustion, time-budget overrun, and crashes inside the recovery
    agent itself. The single effect class lets the rule engine encode
    them with one row each rather than one per failure type.
    """

    reason: FailedReason

    def __post_init__(self) -> None:
        """
        Coerce a string ``reason`` passed by older rule rows into the enum.

        Rule rows persisted before ``FailedReason`` was introduced may
        carry the bare string; coercing in ``__post_init__`` keeps
        downstream effect application typed even on a frozen
        dataclass, avoiding sprinkled ``isinstance`` checks at the
        consumer side.
        """
        if not isinstance(self.reason, FailedReason):
            object.__setattr__(self, "reason", FailedReason(self.reason))

    def __call__(self, state: TaskState, event: Event) -> StateDelta:
        """
        Rule-engine entry for terminal-failure rows.

        Assembles the failed reason / message, reconstructs hook-reject
        tracking so the next runner read sees a coherent picture, and
        — when failing inside ``RECOVERING`` — records the terminated
        ``RecoveryOutcome`` plus a human-readable explanation for the
        operator's failure summary.
        """
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
    """
    Effect for failures that would trigger recovery if any budget remained.

    Fires when ``recovery_budget_available`` says the per-fingerprint
    budget is already spent. Fails the task with
    ``RECOVERY_BUDGET_HIT``, records the terminated outcome on the
    history list, and writes the explanation that ends up in the
    operator's failure summary so it's clear we gave up because the
    same fingerprint had already been retried up to its cap.
    """
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
    """
    Pick the short verdict label persisted on ``RecoveryOutcome`` for a terminal-in-recovering event.

    The terminal ``Fail`` effect uses this so timeline views can
    distinguish "the agent reported failure" from "we hit the budget"
    from "recovery itself crashed" without re-deriving the cause from
    the event class. Defaults to the failed reason's value when no
    more specific label fits.
    """
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
    """
    Human-readable sentence shown in the failure summary for recovery deaths.

    Branches on the failed-reason and the trigger context: blocked on
    a follow-up task, budget exhausted, recovery agent crashed, or
    simply could not restore a runnable path. Lives outside the
    operator's prompt copy so the wording can change without rewriting
    the rule rows; called from the terminal ``Fail`` effect and from
    ``exhaust_recovery_budget``.
    """
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
