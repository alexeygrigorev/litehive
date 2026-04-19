"""Proactive codex quota checking via chatgpt.com API."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import urllib.error
import urllib.request

from heru.quota._shared import UsageStatus, UsageWindow, normalize_reset_at, usage_limit_block_reason

logger = logging.getLogger(__name__)

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_AUTH_PATH = Path.home() / ".codex" / "auth.json"
_CACHE_TTL_SECONDS = 60


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


def _usage_window(window_data: dict) -> UsageWindow:
    return UsageWindow(
        percent_remaining=max(0.0, 100.0 - float(window_data.get("used_percent", 0))),
        reset_at=normalize_reset_at(window_data.get("reset_at")),
    )


def _parse_quota_response(data: dict) -> UsageStatus:
    rate_limit = data.get("rate_limit")
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    primary_data = rate_limit.get("primary_window")
    if not isinstance(primary_data, dict):
        primary_data = {}
    secondary_data = rate_limit.get("secondary_window")
    if not isinstance(secondary_data, dict):
        secondary_data = {}

    hours = _usage_window(primary_data)
    weeks = _usage_window(secondary_data)

    return UsageStatus(
        hours=hours,
        weeks=weeks,
        limit_reached=bool(rate_limit.get("limit_reached", False)) or weeks.used_percent >= 80.0,
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

    fetcher = _fetch if callable(_fetch) else _fetch_quota
    status = fetcher(token)
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
    return usage_limit_block_reason("codex", status)


def reset_cache() -> None:
    """Clear the cached quota status (useful for testing)."""
    global _cached_status
    _cached_status = None
