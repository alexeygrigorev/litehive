"""Engine quota probes and quota-block translation."""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from heru.quota import (
    check_claude_quota,
    check_codex_quota,
    check_copilot_quota,
    check_zai_quota,
)


class QuotaWindow(Protocol):
    reset_at: str | None


class QuotaStatus(Protocol):
    error: str | None

    @property
    def limit_reached(self) -> bool: ...

    @property
    def short_term(self) -> QuotaWindow: ...

    @property
    def long_term(self) -> QuotaWindow: ...


type QuotaChecker = Callable[[], QuotaStatus]


def _parse_datetime_utc(value: str | None) -> datetime | None:
    """
    Parse a quota reset timestamp into a UTC-aware datetime.
    """
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(normalized, "%Y-%m-%d")
        except ValueError:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _quota_block_reason(engine_name: str, status: QuotaStatus) -> tuple[str | None, str | None]:
    """
    Translate a heru quota status into a skip reason and reset-at pair.
    """
    if status.error is not None:
        return None, None
    if not status.limit_reached:
        return None, None
    reset_at = _preferred_quota_reset_at(status)
    if reset_at:
        reset_suffix = f", resets {reset_at}"
    else:
        reset_suffix = ""
    return f"{engine_name} usage limit reached{reset_suffix}", reset_at


def engine_quota_block(
    engine_name: str,
) -> tuple[str | None, datetime | None]:
    """
    Probe the engine's vendor quota and report whether to skip it.

    Returns ``(skip_reason, freeze_until)`` when the engine is currently
    rate-limited; ``(None, None)`` when it is fine or has no probe.
    Called by the engine-selection loop so quota-blocked engines are
    skipped and auto-frozen until the reset time, without having to
    re-probe on the next stage.
    """
    checker = _quota_checker(engine_name)
    if checker is None:
        return None, None
    status = checker()
    reason, reset_at = _quota_block_reason(engine_name, status)
    return reason, _parse_datetime_utc(reset_at)
