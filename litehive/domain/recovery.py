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
    """Types of events that can trigger recovery.

    Used by RecoveryTrigger to classify what type of failure or condition
    initiated a recovery attempt. Each kind may have different recovery
    strategies and budget tracking.
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
    """Final disposition of a recovery attempt.

    Records how a recovery attempt concluded, indicating what happened
    to task execution after recovery was invoked. Used for tracking
    recovery effectiveness and debugging recovery patterns.
    """

    RESUMED = "resumed"  # Task execution resumed from where it left off
    ADVANCED = "advanced"  # Task was moved forward to next stage
    COMPLETED = "completed"  # Task was completed as part of recovery
    TERMINATED = "terminated"  # Task execution was terminated


@dataclass(frozen=True)
class FailureFingerprint:
    """Normalized recovery-budget key plus optional diagnostics.

    Used to identify similar failure patterns for recovery budget tracking.
    Multiple failures with the same fingerprint may exhaust recovery budget
    to prevent infinite recovery loops.

    The budget_key() method determines how recovery budget is tracked:
    uses classification if available, otherwise falls back to fingerprint.

    ``FailureFingerprint`` is the recovery-domain replacement for the older
    document-only idea of a ``FailureDiagnostics`` value object. Diagnostics
    on this object explain the fingerprint; report ``failure_diagnostics``
    fields remain report evidence.
    """

    fingerprint: str  # Unique identifier for this failure pattern
    classification: str | None = None  # Optional failure category for budget grouping
    diagnostics: dict[str, Any] = field(default_factory=dict)  # Additional failure context

    def budget_key(self) -> str:
        return self.classification or self.fingerprint

    def to_payload(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "classification": self.classification,
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FailureFingerprint":
        return cls(
            fingerprint=str(payload.get("fingerprint") or "unknown"),
            classification=payload.get("classification"),
            diagnostics=dict(payload.get("diagnostics") or {}),
        )


@dataclass(frozen=True)
class RecoveryTrigger:
    """Structured description of what sent the task into recovery.

    Created when a failure or condition triggers recovery. Contains all the
    context needed to decide if recovery should be attempted and to track
    recovery budget usage. The budget_key() method provides a scope for
    tracking how many times recovery has been attempted for similar issues.

    Used by RecoveryCoordinator to decide whether to attempt recovery and
    by recovery budget logic to prevent infinite recovery loops.

    This is the recovery context. Code and prompts should use
    ``RecoveryTrigger`` / ``recovery_trigger`` instead of introducing a
    separate ``RecoveryContext`` type or payload.
    """

    origin_stage: str | None  # Pipeline stage where failure occurred
    trigger_event_kind: TriggerEventKind  # Type of event that triggered recovery
    failure_fingerprint: FailureFingerprint  # Normalized failure pattern for budget tracking
    source: str | None = None  # Component that detected the failure
    reason_code: str | None = None  # Machine-readable reason for recovery
    message: str = ""  # Human-readable explanation
    diagnostics: dict[str, Any] = field(default_factory=dict)  # Additional failure context

    def budget_key(self) -> str:
        origin = self.origin_stage or "<unknown>"
        return f"{origin}:{self.failure_fingerprint.budget_key()}"

    def to_payload(self) -> dict[str, Any]:
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
    """Persisted result for one recovery attempt or denial.

    Records what happened when recovery was attempted for a specific trigger.
    Used for recovery budget tracking, debugging recovery patterns, and
    preventing repeated recovery attempts for the same failure pattern.

    Created by RecoveryCoordinator when recovery concludes (successfully
    or unsuccessfully). Stored in TaskState recovery history to track
    all recovery attempts for the task.

    This is the recovery record. Code and docs should use
    ``RecoveryOutcome`` / ``recovery_history`` instead of a separate
    ``RecoveryRecord`` model.
    """

    trigger: RecoveryTrigger  # What triggered this recovery attempt
    recovery_verdict: str  # Final verdict from recovery process
    disposition: RecoveryDisposition  # How the recovery attempt concluded
    reason_code: str | None = None  # Machine-readable outcome classification
    message: str = ""  # Human-readable outcome explanation
    created_at: str = field(default_factory=utcnow)  # When the recovery outcome was recorded

    def to_payload(self) -> dict[str, Any]:
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
    return f"{BLOCKED_ON_FOLLOW_UP_REASON_PREFIX}{follow_up_task_id.strip()}"


def parse_blocked_on_follow_up_reason(reason: str | None) -> str | None:
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
