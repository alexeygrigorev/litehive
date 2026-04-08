"""Proactive Z.AI quota checking via goz CLI."""

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60


@dataclass(slots=True)
class ZaiQuotaWindow:
    used_percent: float = 0.0
    window_hours: int = 0
    remaining: int = 0
    limit: int = 0


@dataclass(slots=True)
class ZaiQuotaStatus:
    limit_reached: bool = False
    api_calls: ZaiQuotaWindow = field(default_factory=ZaiQuotaWindow)
    tokens: ZaiQuotaWindow = field(default_factory=ZaiQuotaWindow)
    checked_at: float = 0.0
    error: str | None = None

    @property
    def max_used_percent(self) -> float:
        return max(self.api_calls.used_percent, self.tokens.used_percent)


_cached_status: ZaiQuotaStatus | None = None


def _fetch_usage(*, timeout: float = 10.0) -> ZaiQuotaStatus:
    try:
        result = subprocess.run(
            ["goz", "usage", "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return ZaiQuotaStatus(checked_at=time.monotonic(), error=f"goz exit {result.returncode}")
        data = json.loads(result.stdout)
    except FileNotFoundError:
        return ZaiQuotaStatus(checked_at=time.monotonic(), error="goz not on PATH")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("zai quota check failed (fail-open): %s", exc)
        return ZaiQuotaStatus(checked_at=time.monotonic(), error=str(exc))

    api_calls = ZaiQuotaWindow()
    tokens = ZaiQuotaWindow()

    for limit in data.get("limits", []):
        window = ZaiQuotaWindow(
            used_percent=float(limit.get("percentage", 0)),
            window_hours=limit.get("window_hours", 0),
            remaining=limit.get("remaining", 0),
            limit=limit.get("limit", 0),
        )
        if limit.get("type") == "TIME_LIMIT":
            api_calls = window
        elif limit.get("type") == "TOKENS_LIMIT":
            tokens = window

    limit_reached = api_calls.used_percent >= 80 or tokens.used_percent >= 80

    return ZaiQuotaStatus(
        limit_reached=limit_reached,
        api_calls=api_calls,
        tokens=tokens,
        checked_at=time.monotonic(),
    )


def check_zai_quota(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> ZaiQuotaStatus:
    """Check Z.AI quota via goz CLI. Returns cached result within TTL. Fails open."""
    global _cached_status
    if _cached_status is not None and time.monotonic() - _cached_status.checked_at < cache_ttl:
        return _cached_status

    fetcher = _fetch or _fetch_usage
    _cached_status = fetcher()
    return _cached_status


def zai_quota_block_reason(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> str | None:
    """Return a blocking reason if Z.AI quota is reached, or None."""
    status = check_zai_quota(cache_ttl=cache_ttl, _fetch=_fetch)
    if status.error:
        return None  # fail-open
    if status.limit_reached:
        pct = status.max_used_percent
        return f"zai usage limit reached ({pct:.0f}% used)"
    return None


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
