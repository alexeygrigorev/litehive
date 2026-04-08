"""Proactive Copilot quota checking via GitHub API."""

import json
import logging
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60


@dataclass(slots=True)
class CopilotQuotaStatus:
    limit_reached: bool = False
    premium_remaining: int = 0
    premium_entitlement: int = 0
    premium_percent_remaining: float = 100.0
    quota_reset_date: str | None = None
    checked_at: float = 0.0
    error: str | None = None

    @property
    def used_percent(self) -> float:
        return 100.0 - self.premium_percent_remaining


_cached_status: CopilotQuotaStatus | None = None


def _fetch_quota(*, timeout: float = 10.0) -> CopilotQuotaStatus:
    try:
        result = subprocess.run(
            ["gh", "api", "/copilot_internal/user"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return CopilotQuotaStatus(checked_at=time.monotonic(), error=f"gh exit {result.returncode}")
        data = json.loads(result.stdout)
    except FileNotFoundError:
        return CopilotQuotaStatus(checked_at=time.monotonic(), error="gh not on PATH")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("copilot quota check failed (fail-open): %s", exc)
        return CopilotQuotaStatus(checked_at=time.monotonic(), error=str(exc))

    snapshots = data.get("quota_snapshots", {})
    premium = snapshots.get("premium_interactions", {})

    if premium.get("unlimited", False):
        return CopilotQuotaStatus(
            premium_percent_remaining=100.0,
            checked_at=time.monotonic(),
        )

    remaining = int(premium.get("remaining", 0))
    entitlement = int(premium.get("entitlement", 0))
    pct_remaining = float(premium.get("percent_remaining", 100.0))
    limit_reached = pct_remaining <= 20

    return CopilotQuotaStatus(
        limit_reached=limit_reached,
        premium_remaining=remaining,
        premium_entitlement=entitlement,
        premium_percent_remaining=pct_remaining,
        quota_reset_date=data.get("quota_reset_date"),
        checked_at=time.monotonic(),
    )


def check_copilot_quota(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> CopilotQuotaStatus:
    """Check Copilot quota via gh CLI. Returns cached result within TTL. Fails open."""
    global _cached_status
    if _cached_status is not None and time.monotonic() - _cached_status.checked_at < cache_ttl:
        return _cached_status

    fetcher = _fetch or _fetch_quota
    _cached_status = fetcher()
    return _cached_status


def copilot_quota_block_reason(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> str | None:
    """Return a blocking reason if Copilot quota is reached, or None."""
    status = check_copilot_quota(cache_ttl=cache_ttl, _fetch=_fetch)
    if status.error:
        return None  # fail-open
    if status.limit_reached:
        return (
            f"copilot premium requests low ({status.premium_remaining}/{status.premium_entitlement} "
            f"remaining, {status.premium_percent_remaining:.0f}%, resets {status.quota_reset_date})"
        )
    return None


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
