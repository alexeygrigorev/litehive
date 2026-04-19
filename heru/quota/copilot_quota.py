"""Proactive Copilot quota checking via GitHub API."""

import json
import logging
import subprocess
import time

from heru.quota._shared import UsageStatus, UsageWindow, normalize_reset_at, usage_limit_block_reason

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60


_cached_status: UsageStatus | None = None


def _fetch_quota(*, timeout: float = 10.0) -> UsageStatus:
    try:
        result = subprocess.run(
            ["gh", "api", "/copilot_internal/user"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return UsageStatus(checked_at=time.monotonic(), error=f"gh exit {result.returncode}")
        data = json.loads(result.stdout)
    except FileNotFoundError:
        return UsageStatus(checked_at=time.monotonic(), error="gh not on PATH")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("copilot quota check failed (fail-open): %s", exc)
        return UsageStatus(checked_at=time.monotonic(), error=str(exc))

    snapshots = data.get("quota_snapshots")
    if not isinstance(snapshots, dict):
        snapshots = {}
    premium = snapshots.get("premium_interactions")
    if not isinstance(premium, dict):
        premium = {}

    if premium.get("unlimited", False):
        return UsageStatus(checked_at=time.monotonic())

    percent_remaining = max(0.0, float(premium.get("percent_remaining", 100.0)))
    reset_at = normalize_reset_at(data.get("quota_reset_date"))

    return UsageStatus(
        weeks=UsageWindow(percent_remaining=percent_remaining, reset_at=reset_at),
        limit_reached=percent_remaining <= 20.0,
        checked_at=time.monotonic(),
    )


def check_copilot_quota(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> UsageStatus:
    """Check Copilot quota via gh CLI. Returns cached result within TTL. Fails open."""
    global _cached_status
    if _cached_status is not None and time.monotonic() - _cached_status.checked_at < cache_ttl:
        return _cached_status

    fetcher = _fetch if callable(_fetch) else _fetch_quota
    _cached_status = fetcher()
    return _cached_status


def copilot_quota_block_reason(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> str | None:
    """Return a blocking reason if Copilot quota is reached, or None."""
    status = check_copilot_quota(cache_ttl=cache_ttl, _fetch=_fetch)
    return usage_limit_block_reason("copilot", status)


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
