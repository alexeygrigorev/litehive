"""Shared quota status models and normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class UsageWindow:
    percent_remaining: float = 100.0
    reset_at: str | None = None

    @property
    def used_percent(self) -> float:
        return max(0.0, 100.0 - self.percent_remaining)


@dataclass(slots=True)
class UsageStatus:
    limit_reached: bool = False
    hours: UsageWindow = field(default_factory=UsageWindow)
    weeks: UsageWindow = field(default_factory=UsageWindow)
    checked_at: float = 0.0
    error: str | None = None


def usage_windows(status: UsageStatus) -> tuple[tuple[str, UsageWindow], tuple[str, UsageWindow]]:
    return (("hours", status.hours), ("weeks", status.weeks))


def most_constrained_window(status: UsageStatus) -> tuple[str, UsageWindow]:
    return max(usage_windows(status), key=lambda item: item[1].used_percent)


def preferred_reset_at(status: UsageStatus) -> str | None:
    return status.weeks.reset_at or status.hours.reset_at


def usage_limit_block_reason(engine_name: str, status: UsageStatus) -> str | None:
    if status.error is not None or not status.limit_reached:
        return None
    window_name, window = most_constrained_window(status)
    reset_info = f", resets {window.reset_at}" if window.reset_at else ""
    return f"{engine_name} usage limit reached ({window_name} window at {window.used_percent:.0f}%{reset_info})"


def normalize_reset_at(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = str(value).strip()
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
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
