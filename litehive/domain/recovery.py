"""Recovery-domain enums and persisted value objects."""

from dataclasses import dataclass, field
from typing import Any

from .common import StringEnum, utcnow


class TriggerEventKind(StringEnum):
    REJECT = "reject"
    BLOCKED = "blocked"
    CRASH = "crash"
    TIMEOUT = "timeout"
    STAGE_RETRY_LIMIT = "stage_retry_limit"
    RETRY_LIMIT = "retry_limit"
    FLAGGED_TASK = "flagged_task"
    STALE_RUNNER_RECOVERY = "stale_runner_recovery"
    UNKNOWN = "unknown"


class RecoveryDisposition(StringEnum):
    RESUMED = "resumed"
    ADVANCED = "advanced"
    COMPLETED = "completed"
    TERMINATED = "terminated"


@dataclass(frozen=True)
class FailureFingerprint:
    """Normalized recovery-budget key plus optional diagnostics."""

    fingerprint: str
    classification: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

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
    """Structured description of what sent the task into recovery."""

    origin_stage: str | None
    trigger_event_kind: TriggerEventKind
    failure_fingerprint: FailureFingerprint
    source: str | None = None
    reason_code: str | None = None
    message: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

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
            failure_fingerprint=FailureFingerprint.from_payload(
                dict(payload.get("failure_fingerprint") or {})
            ),
            source=payload.get("source"),
            reason_code=payload.get("reason_code"),
            message=str(payload.get("message") or ""),
            diagnostics=dict(payload.get("diagnostics") or {}),
        )


@dataclass(frozen=True)
class RecoveryOutcome:
    """Persisted result for one recovery attempt or denial."""

    trigger: RecoveryTrigger
    recovery_verdict: str
    disposition: RecoveryDisposition
    reason_code: str | None = None
    message: str = ""
    created_at: str = field(default_factory=utcnow)

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
            disposition=RecoveryDisposition(
                payload.get("disposition") or RecoveryDisposition.TERMINATED
            ),
            reason_code=payload.get("reason_code"),
            message=str(payload.get("message") or ""),
            created_at=str(payload.get("created_at") or utcnow()),
        )


__all__ = [
    "FailureFingerprint",
    "RecoveryDisposition",
    "RecoveryOutcome",
    "RecoveryTrigger",
    "TriggerEventKind",
]
