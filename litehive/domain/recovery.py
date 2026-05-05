"""Recovery-domain enums and persisted value objects.

Canonical recovery vocabulary:

- ``FailureFingerprint`` is the normalized identity used for recovery
  budgets and repeated-failure detection. It may carry small diagnostics
  that explain how the fingerprint was derived.
- ``RecoveryTrigger`` is the active cause/context that sent a task into
  recovery. It is persisted as ``active_recovery_trigger`` and surfaced to
  recovery prompts as ``recovery_trigger``.
- ``RecoveryOutcome`` is one completed recovery attempt or denial. Outcomes
  are persisted in ``recovery_history`` and projected into
  ``RuntimeRecoveryOutcome`` on task runtime.

Model relationships:
- ``RecoveryTrigger`` CONTAINS a ``FailureFingerprint`` to identify the
  failure pattern and enable budget tracking across similar failures.
- ``RecoveryOutcome`` CONTAINS a ``RecoveryTrigger`` to record what originally
  triggered the recovery attempt that this outcome describes.
- Collections: ``recovery_history`` contains multiple ``RecoveryOutcome``
  instances; each task has one active ``RecoveryTrigger`` at most.

There is no separate ``FailureDiagnostics``, ``RecoveryContext``, or
``RecoveryRecord`` model. Report-level ``failure_diagnostics`` fields remain
unstructured evidence on reports/outcomes; they are not the recovery identity.
"""

from dataclasses import dataclass, field
from typing import Any

from .common import StringEnum, utcnow


class TriggerEventKind(StringEnum):
    """
    Categories the recovery agent prompt branches on.

    The recovery template picks a diagnosis branch based on the kind:
    a ``REJECT`` is investigated very differently from a ``CRASH`` or
    a ``TIMEOUT``. Carrying a typed enum keeps the prompt and the
    rule engine in agreement instead of letting either side string-
    compare against a moving Python class name.
    """

    REJECT = "reject"  # Stage verdict was reject
    SEMANTIC_REJECT = "semantic_reject"  # QA/reviewer judgment-based reject
    BLOCKED = "blocked"  # Stage verdict was blocked
    CRASH = "crash"  # Subagent or system crashed
    TIMEOUT = "timeout"  # Operation timed out
    STAGE_RETRY_LIMIT = "stage_retry_limit"  # Per-stage retry limit exceeded
    RETRY_LIMIT = "retry_limit"  # Overall retry limit exceeded
    FLAGGED_TASK = "flagged_task"  # Task was flagged for operator attention
    STALE_RUNNER_RECOVERY = "stale_runner_recovery"  # Runner became unresponsive
    UNKNOWN = "unknown"  # Unclassified recovery trigger


class RecoveryDisposition(StringEnum):
    """
    How a recovery attempt concluded.

    The lifecycle layer writes one of these on every appended
    ``RecoveryOutcome`` so timeline views can render "recovery said
    resume" vs "recovery advanced past the failed stage" vs "recovery
    completed the task" vs "recovery gave up and the task terminated"
    without re-deriving the disposition from the verdict string.
    """

    RESUMED = "resumed"  # Task execution resumed from where it left off
    ADVANCED = "advanced"  # Task was moved forward to next stage
    COMPLETED = "completed"  # Task was completed as part of recovery
    TERMINATED = "terminated"  # Task execution was terminated


@dataclass(frozen=True)
class FailureFingerprint:
    """
    Normalized recovery-budget key plus the diagnostics that explain it.

    The recovery budget caps how many attempts a single fingerprint
    may consume — without that cap, the same persistent failure would
    loop forever. ``budget_key()`` collapses to ``classification`` when
    set so a family of related rejects shares one budget bucket; the
    ``diagnostics`` dict is informational only (helps the recovery
    agent reason about why the fingerprint was assembled this way).
    """

    fingerprint: str  # Unique identifier for this failure pattern
    classification: str | None = None  # Optional failure category for budget grouping
    diagnostics: dict[str, Any] = field(default_factory=dict)  # Additional failure context

    def budget_key(self) -> str:
        """
        Collapse classification or raw fingerprint into the budget bucket key.

        Prefers ``classification`` when set so related failures (e.g.
        all "type-check failed" rejects) share one budget; falls back
        to the raw fingerprint when no classification was attached.
        Used by ``RecoveryTrigger.budget_key`` and the per-fingerprint
        budget guard.
        """
        return self.classification or self.fingerprint

    def to_payload(self) -> dict[str, Any]:
        """
        Serialize to the JSON shape persisted in lifecycle recovery history.

        Mirrors :meth:`from_payload` so the round-trip stays lossless.
        Diagnostics is copied (not aliased) to keep the persisted
        snapshot independent of the live object.
        """
        return {
            "fingerprint": self.fingerprint,
            "classification": self.classification,
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FailureFingerprint":
        """
        Rehydrate from a persisted lifecycle entry.

        Tolerates missing fingerprint by defaulting to ``"unknown"``
        rather than raising — older recovery rows may have been written
        before fingerprinting was wired in, and we want loading to
        succeed so the operator can still read history.
        """
        return cls(
            fingerprint=str(payload.get("fingerprint") or "unknown"),
            classification=payload.get("classification"),
            diagnostics=dict(payload.get("diagnostics") or {}),
        )


@dataclass(frozen=True)
class RecoveryTrigger:
    """
    Structured description of what sent the task into recovery.

    Carries everything two callers need: the budget guard reads
    ``budget_key()`` to look up the per-fingerprint cap, and the
    recovery agent reads ``origin_stage`` / ``trigger_event_kind`` /
    ``message`` from prompt context to diagnose the failure. Storing
    one object on ``state.active_recovery_trigger`` keeps both views
    in sync — a separate ``RecoveryContext`` type would invite drift.
    """

    origin_stage: str | None  # Pipeline stage where failure occurred
    trigger_event_kind: TriggerEventKind  # Type of event that triggered recovery
    failure_fingerprint: FailureFingerprint  # Normalized failure pattern for budget tracking
    source: str | None = None  # Component that detected the failure
    reason_code: str | None = None  # Machine-readable reason for recovery
    message: str = ""  # Human-readable explanation
    diagnostics: dict[str, Any] = field(default_factory=dict)  # Additional failure context

    def budget_key(self) -> str:
        """
        Compose origin stage with the fingerprint so budgets are stage-scoped.

        Without the stage prefix a fingerprint that recurs in two
        different stages would consume one shared budget — this would
        let a flaky test phase exhaust the budget for an unrelated
        commit-stage failure. Stage-scoping fixes that.
        """
        origin = self.origin_stage or "<unknown>"
        return f"{origin}:{self.failure_fingerprint.budget_key()}"

    def to_payload(self) -> dict[str, Any]:
        """
        Serialize for persistence on ``state.active_recovery_trigger``.

        Mirrors :meth:`from_payload`; ``diagnostics`` is copied so the
        persisted snapshot is independent of the live in-memory dict.
        """
        return {
            "origin_stage": self.origin_stage,
            "trigger_event_kind": self.trigger_event_kind.value,
            "failure_fingerprint": self.failure_fingerprint.to_payload(),
            "source": self.source,
            "reason_code": self.reason_code,
            "message": self.message,
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RecoveryTrigger":
        """
        Rehydrate from persisted lifecycle state.

        Treats unknown ``trigger_event_kind`` strings as ``UNKNOWN``
        rather than raising so a forward-rev rule row that introduced
        a new kind doesn't break loading on the older binary; the
        downstream branches all have an ``UNKNOWN`` fallback.
        """
        return cls(
            origin_stage=payload.get("origin_stage"),
            trigger_event_kind=TriggerEventKind(payload.get("trigger_event_kind") or TriggerEventKind.UNKNOWN),
            failure_fingerprint=FailureFingerprint.from_payload(dict(payload.get("failure_fingerprint") or {})),
            source=payload.get("source"),
            reason_code=payload.get("reason_code"),
            message=str(payload.get("message") or ""),
            diagnostics=dict(payload.get("diagnostics") or {}),
        )


@dataclass(frozen=True)
class RecoveryOutcome:
    """
    Persisted result for one recovery attempt (or denial).

    Lifecycle effects append one of these to ``state.recovery_history``
    every time recovery concludes — successful resume, terminated, or
    refused for budget. The history backs the recovery-budget guard
    (counts entries per fingerprint), the operator's recovery timeline,
    and the runtime projection ``RuntimeRecoveryOutcome`` shown on
    status.
    """

    trigger: RecoveryTrigger  # What triggered this recovery attempt
    recovery_verdict: str  # Final verdict from recovery process
    disposition: RecoveryDisposition  # How the recovery attempt concluded
    reason_code: str | None = None  # Machine-readable outcome classification
    message: str = ""  # Human-readable outcome explanation
    created_at: str = field(default_factory=utcnow)  # When the recovery outcome was recorded

    def to_payload(self) -> dict[str, Any]:
        """
        Serialize a completed recovery attempt for the history list.

        Mirrors :meth:`from_payload`; the trigger is recursively
        flattened so the persisted form is fully self-describing
        without any pickled object references.
        """
        return {
            "trigger": self.trigger.to_payload(),
            "recovery_verdict": self.recovery_verdict,
            "disposition": self.disposition.value,
            "reason_code": self.reason_code,
            "message": self.message,
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RecoveryOutcome":
        """
        Rehydrate from a persisted ``recovery_history`` entry.

        Defaults missing ``disposition`` to ``TERMINATED`` so older
        rows written before the disposition column existed read back
        as the conservative case (they were ones we did not resume
        from), rather than raising and breaking history loading.
        """
        return cls(
            trigger=RecoveryTrigger.from_payload(dict(payload.get("trigger") or {})),
            recovery_verdict=str(payload.get("recovery_verdict") or ""),
            disposition=RecoveryDisposition(payload.get("disposition") or RecoveryDisposition.TERMINATED),
            reason_code=payload.get("reason_code"),
            message=str(payload.get("message") or ""),
            created_at=str(payload.get("created_at") or utcnow()),
        )


BLOCKED_ON_FOLLOW_UP_REASON_PREFIX = "blocked_on_follow_up:"


def blocked_on_follow_up_reason(follow_up_task_id: str) -> str:
    """
    Encode the parent's "waiting on child task" reason string.

    Used when an agent spawns a blocking follow-up: the parent's
    ``reason`` becomes ``blocked_on_follow_up:<id>`` so the runner can
    later parse the child id back out and check whether the child has
    landed before letting the parent resume. Pairs with
    :func:`parse_blocked_on_follow_up_reason` — encoding lives in one
    place so both sides cannot drift.
    """
    return f"{BLOCKED_ON_FOLLOW_UP_REASON_PREFIX}{follow_up_task_id.strip()}"


def parse_blocked_on_follow_up_reason(reason: str | None) -> str | None:
    """
    Recover the follow-up task id from a parent's blocked reason.

    Inverse of :func:`blocked_on_follow_up_reason`. Returns ``None``
    when the reason isn't this specific shape so the caller can keep
    a single decoder for all blocked-reason variants. The runner uses
    it when deciding whether the parent's child has landed and the
    parent can resume.
    """
    if reason is None:
        return None
    normalized = reason.strip()
    if not normalized.startswith(BLOCKED_ON_FOLLOW_UP_REASON_PREFIX):
        return None
    follow_up_task_id = normalized.removeprefix(BLOCKED_ON_FOLLOW_UP_REASON_PREFIX).strip()
    return follow_up_task_id or None


__all__ = [
    "BLOCKED_ON_FOLLOW_UP_REASON_PREFIX",
    "FailureFingerprint",
    "RecoveryDisposition",
    "RecoveryOutcome",
    "RecoveryTrigger",
    "TriggerEventKind",
    "blocked_on_follow_up_reason",
    "parse_blocked_on_follow_up_reason",
]
