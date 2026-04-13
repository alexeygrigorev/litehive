"""Proactive codex quota checking via chatgpt.com API."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import urllib.request
import urllib.error

from heru.quota._shared import UsageStatus, UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_AUTH_PATH = Path.home() / ".codex" / "auth.json"
_CACHE_TTL_SECONDS = 60


# Module-level cache
_cached_status: UsageStatus | None = None


def _read_bearer_token(auth_path: Path | None = None) -> str | None:
    path = auth_path or _AUTH_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = data.get("tokens", {}).get("access_token")
        if token:
            return token
        logger.warning("codex auth.json missing tokens.access_token")
        return None
    except FileNotFoundError:
        logger.warning("codex auth.json not found at %s", path)
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("codex auth.json parse error: %s", exc)
        return None


def _parse_quota_response(data: dict) -> UsageStatus:
    rate_limit = data.get("rate_limit", {})
    weekly_data = rate_limit.get("secondary_window", {})
    weekly_used_percent = float(weekly_data.get("used_percent", 0))
    long_term = UsageWindow(
        percent_remaining=max(0.0, 100.0 - weekly_used_percent),
        reset_at=normalize_reset_at(weekly_data.get("reset_at")),
    )

    return UsageStatus(
        limit_reached=long_term.used_percent >= 80.0,
        short_term=UsageWindow(percent_remaining=100.0),
        long_term=long_term,
        checked_at=time.monotonic(),
    )


def _fetch_quota(token: str, *, timeout: float = 10.0) -> UsageStatus:
    req = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_quota_response(data)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        logger.warning("codex quota check failed (fail-open): %s", exc)
        return UsageStatus(checked_at=time.monotonic(), error=str(exc))


def check_codex_quota(
    *,
    auth_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> UsageStatus:
    """Check codex quota proactively. Returns cached result within TTL.

    Fails open: if auth is missing or API call fails, returns a non-blocking status.
    """
    global _cached_status
    now = time.monotonic()

    if _cached_status is not None and (now - _cached_status.checked_at) < cache_ttl:
        return _cached_status

    token = _read_bearer_token(auth_path)
    if token is None:
        status = UsageStatus(checked_at=now, error="no auth token")
        _cached_status = status
        return status

    if callable(_fetch):
        status = _fetch(token)
    else:
        status = _fetch_quota(token)

    _cached_status = status
    return status


def codex_quota_block_reason(
    *,
    auth_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> str | None:
    """Return a blocking reason string if codex quota is exhausted, or None if OK."""
    status = check_codex_quota(auth_path=auth_path, cache_ttl=cache_ttl, _fetch=_fetch)
    if status.error is not None:
        return None  # fail-open
    if status.limit_reached:
        reset_info = f", resets {status.long_term.reset_at}" if status.long_term.reset_at else ""
        return f"codex quota exhausted (weekly window at {status.long_term.used_percent:.0f}%{reset_info})"
    return None


def reset_cache() -> None:
    """Clear the cached quota status (useful for testing)."""
    global _cached_status
    _cached_status = None
