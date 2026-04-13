"""Proactive Claude quota checking via OAuth usage endpoint."""

import json
import logging
import os
import time
from pathlib import Path

import urllib.request
import urllib.error

from heru.quota._shared import UsageStatus, UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CACHE_TTL_SECONDS = 60


def _default_credentials_path() -> Path:
    """Resolve Claude credentials path, respecting config dir overrides."""
    # Claude Code may use a custom config dir
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


# Module-level cache
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


def _parse_usage_response(data: dict) -> UsageStatus:
    five_hour_data = data.get("five_hour") or {}
    seven_day_data = data.get("seven_day") or {}

    short_term = UsageWindow(
        percent_remaining=max(0.0, 100.0 - float(five_hour_data.get("utilization", 0))),
        reset_at=normalize_reset_at(five_hour_data.get("resets_at")),
    )
    long_term = UsageWindow(
        percent_remaining=max(0.0, 100.0 - float(seven_day_data.get("utilization", 0))),
        reset_at=normalize_reset_at(seven_day_data.get("resets_at")),
    )

    limit_reached = long_term.percent_remaining <= 5.0

    return UsageStatus(
        limit_reached=limit_reached,
        short_term=short_term,
        long_term=long_term,
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

    fetcher = _fetch or _fetch_usage
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
    if status.error:
        return None  # fail-open
    if status.limit_reached:
        return (
            f"claude usage limit reached "
            f"(long-term window at {status.long_term.used_percent:.0f}%, resets {status.long_term.reset_at})"
        )
    return None


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
