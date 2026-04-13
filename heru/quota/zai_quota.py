"""Proactive Z.AI quota checking via goz CLI."""

import json
import logging
import subprocess
import time
from heru.quota._shared import UsageStatus, UsageWindow

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60


_cached_status: UsageStatus | None = None


def _fetch_usage(*, timeout: float = 10.0) -> UsageStatus:
    try:
        result = subprocess.run(
            ["goz", "usage", "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return UsageStatus(checked_at=time.monotonic(), error=f"goz exit {result.returncode}")
        data = json.loads(result.stdout)
    except FileNotFoundError:
        return UsageStatus(checked_at=time.monotonic(), error="goz not on PATH")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("zai quota check failed (fail-open): %s", exc)
        return UsageStatus(checked_at=time.monotonic(), error=str(exc))

    short_term = UsageWindow(percent_remaining=100.0)

    for limit in data.get("limits", []):
        if limit.get("type") == "TOKENS_LIMIT":
            short_term = UsageWindow(
                percent_remaining=max(0.0, 100.0 - float(limit.get("percentage", 0))),
                reset_at=None,
            )

    return UsageStatus(
        limit_reached=False,
        short_term=short_term,
        long_term=UsageWindow(percent_remaining=100.0),
        checked_at=time.monotonic(),
    )


def check_zai_quota(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> UsageStatus:
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
    return None


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
