"""Shared timestamp parsing helpers for config-owned runtime settings."""

from datetime import datetime, timezone


def parse_utc_datetime(value: str | None) -> datetime | None:
    """
    Parse a stored UTC timestamp into an aware ``datetime``.

    Accepts ISO 8601 timestamps with optional trailing ``Z`` and the
    legacy ``YYYY-MM-DD`` form. Returns ``None`` for absent or
    unparseable input so callers can treat invalid persisted values as
    inactive instead of leaking storage errors into selection paths.
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


def parse_engine_freeze_until(value: str | None) -> str | None:
    """
    Convert a ``YYYY-MM-DD`` date into the persisted UTC ISO form.

    Returns ``None`` for unparseable input so CLI callers can surface a
    single validation error before writing runtime settings.
    """
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
