"""Engine quota probes and quota-block translation."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from heru.quota import (
    UsageStatus,
    check_claude_quota,
    check_codex_quota,
    check_copilot_quota,
    check_zai_quota,
)
from litehive.config.time_parsing import parse_utc_datetime


class QuotaWindow(Protocol):
    """
    Protocol for one quota reset window (short-term or long-term).

    Vendors return these as part of a quota status so Litehive can
    auto-freeze an engine until the reset time instead of re-probing
    on every stage.
    """

    reset_at: str | None


class QuotaStatus(Protocol):
    """
    Protocol for the result of a vendor quota probe.

    Abstracts over ``heru.quota.UsageStatus`` so the config layer can
    inspect quota state without importing the heru models directly.
    """

    error: str | None

    @property
    def limit_reached(self) -> bool: ...

    @property
    def short_term(self) -> QuotaWindow: ...

    @property
    def long_term(self) -> QuotaWindow: ...


QuotaChecker = Callable[[], QuotaStatus]


@dataclass(frozen=True, slots=True)
class EngineQuotaBlock:
    """
    Present quota block returned by vendor quota probes.

    Attributes:
        reason: Operator-facing skip reason for the quota-blocked engine.
        freeze_until: UTC reset time when known; ``None`` keeps the block
            transient and avoids persisting a freeze window.
    """

    reason: str
    freeze_until: datetime | None


def _quota_checker(engine_name: str) -> QuotaChecker | None:
    """
    Return the heru ``check_*_quota`` callable for an engine.

    Returns ``None`` for engines without a quota probe (Gemini today).
    The dispatch table lets :func:`engine_quota_block` stay generic
    across engines instead of growing a chain of ``if engine_name == ...``
    branches at the call site.
    """
    if engine_name == "codex":
        return check_codex_quota
    if engine_name == "claude":
        return check_claude_quota
    if engine_name == "copilot":
        return check_copilot_quota
    if engine_name in ("goz", "opencode"):
        return check_zai_quota
    return None


def _preferred_quota_reset_at(status: QuotaStatus) -> str | None:
    """
    Ask heru for the most informative reset timestamp on a quota status.
    """
    if status.long_term.reset_at:
        return status.long_term.reset_at
    return status.short_term.reset_at


def _quota_block_reason(engine_name: str, status: QuotaStatus) -> EngineQuotaBlock | None:
    """
    Translate a heru quota status into a skip reason and freeze window.
    """
    if status.error is not None:
        return None
    if not status.limit_reached:
        return None
    reset_at = _preferred_quota_reset_at(status)
    if reset_at:
        reset_suffix = f", resets {reset_at}"
    else:
        reset_suffix = ""
    return EngineQuotaBlock(
        reason=f"{engine_name} usage limit reached{reset_suffix}",
        freeze_until=parse_utc_datetime(reset_at),
    )


def engine_quota_block(
    engine_name: str,
) -> EngineQuotaBlock | None:
    """
    Probe the engine's vendor quota and report whether to skip it.

    Returns a present block when the engine is currently rate-limited,
    or ``None`` when it is fine or has no probe. Called by the
    engine-selection loop so quota-blocked engines are skipped and
    auto-frozen until the reset time, without having to re-probe on the
    next stage.
    """
    checker = _quota_checker(engine_name)
    if checker is None:
        return None
    status = checker()
    return _quota_block_reason(engine_name, status)


def collect_engine_quota_statuses() -> dict[str, object]:
    """
    Probe each supported provider's quota once for engine status output.

    Returns a dict keyed by engine name so callers can render one row
    per engine without paying the provider probe cost repeatedly.
    Z.ai's probe covers both ``goz`` and ``opencode`` because they
    share a backend; ``gemini`` has no probe surface.
    """
    claude_status = _safe_quota_check(check_claude_quota)
    codex_status = _safe_quota_check(check_codex_quota)
    copilot_status = _safe_quota_check(check_copilot_quota)
    zai_status = _safe_quota_check(check_zai_quota)
    gemini_status = "unsupported"
    return {
        "claude": claude_status,
        "codex": codex_status,
        "copilot": copilot_status,
        "gemini": gemini_status,
        "goz": zai_status,
        "opencode": zai_status,
    }


def _safe_quota_check(checker: Callable[[], object]) -> object:
    """
    Run a quota probe without letting one provider break the status table.
    """
    try:
        return checker()
    except Exception as exc:  # pragma: no cover - defensive fallback
        return UsageStatus(error=_quota_error_label(exc))


def _quota_error_label(exc: Exception) -> str:
    """
    Pick a human-readable label for a failed quota probe.
    """
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__
