"""Proactive Claude quota checking via OAuth usage endpoint."""

import json
import logging
import os
import time
from pathlib import Path

import urllib.error
import urllib.request

from heru.quota._shared import UsageStatus, UsageWindow, normalize_reset_at, usage_limit_block_reason

logger = logging.getLogger(__name__)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CACHE_TTL_SECONDS = 60


def _default_credentials_path() -> Path:
    """Resolve Claude credentials path, respecting config dir overrides."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


_cached_status: UsageStatus | None = None


def _read_access_token(creds_path: Path | None = None) -> str | None:
    path = creds_path or _default_credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        if token:
            return token
        logger.warning("claude credentials missing claudeAiOauth.accessToken")
        return None
    except FileNotFoundError:
        logger.warning("claude credentials not found at %s", path)
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("claude credentials parse error: %s", exc)
        return None


def _usage_window(window_data: dict) -> UsageWindow:
    return UsageWindow(
        percent_remaining=max(0.0, 100.0 - float(window_data.get("utilization", 0))),
        reset_at=normalize_reset_at(window_data.get("resets_at")),
    )


def _parse_usage_response(data: dict) -> UsageStatus:
    five_hour_data = data.get("five_hour")
    if not isinstance(five_hour_data, dict):
        five_hour_data = {}
    seven_day_data = data.get("seven_day")
    if not isinstance(seven_day_data, dict):
        seven_day_data = {}

    hours = _usage_window(five_hour_data)
    weeks = _usage_window(seven_day_data)

    return UsageStatus(
        hours=hours,
        weeks=weeks,
        limit_reached=weeks.percent_remaining <= 5.0,
        checked_at=time.monotonic(),
    )


def _fetch_usage(token: str, *, timeout: float = 10.0) -> UsageStatus:
    req = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_usage_response(data)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        logger.warning("claude quota check failed (fail-open): %s", exc)
        return UsageStatus(checked_at=time.monotonic(), error=str(exc))


def check_claude_quota(
    *,
    creds_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> UsageStatus:
    """Check Claude quota proactively. Returns cached result within TTL.

    Fails open: if auth is missing or API call fails, returns a non-blocking status.
    """
    global _cached_status
    if _cached_status is not None and time.monotonic() - _cached_status.checked_at < cache_ttl:
        return _cached_status

    token = _read_access_token(creds_path)
    if token is None:
        return UsageStatus(checked_at=time.monotonic(), error="no-credentials")

    fetcher = _fetch if callable(_fetch) else _fetch_usage
    _cached_status = fetcher(token)
    return _cached_status


def claude_quota_block_reason(
    *,
    creds_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> str | None:
    """Return a blocking reason string if Claude quota is reached, or None."""
    status = check_claude_quota(creds_path=creds_path, cache_ttl=cache_ttl, _fetch=_fetch)
    return usage_limit_block_reason("claude", status)


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
