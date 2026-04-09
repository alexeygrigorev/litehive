"""Proactive Claude quota checking via OAuth usage endpoint."""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import urllib.request
import urllib.error

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


@dataclass(slots=True)
class ClaudeQuotaWindow:
    used_percent: float = 0.0
    reset_at: str | None = None


@dataclass(slots=True)
class ClaudeQuotaStatus:
    limit_reached: bool = False
    five_hour: ClaudeQuotaWindow = field(default_factory=ClaudeQuotaWindow)
    seven_day: ClaudeQuotaWindow = field(default_factory=ClaudeQuotaWindow)
    checked_at: float = 0.0
    error: str | None = None
    subscription: str | None = None

    @property
    def max_used_percent(self) -> float:
        return max(self.five_hour.used_percent, self.seven_day.used_percent)


# Module-level cache
_cached_status: ClaudeQuotaStatus | None = None


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


def _parse_usage_response(data: dict) -> ClaudeQuotaStatus:
    five_hour_data = data.get("five_hour") or {}
    seven_day_data = data.get("seven_day") or {}

    five_hour = ClaudeQuotaWindow(
        used_percent=float(five_hour_data.get("utilization", 0)),
        reset_at=five_hour_data.get("resets_at"),
    )
    seven_day = ClaudeQuotaWindow(
        used_percent=float(seven_day_data.get("utilization", 0)),
        reset_at=seven_day_data.get("resets_at"),
    )

    limit_reached = five_hour.used_percent >= 80 or seven_day.used_percent >= 95

    return ClaudeQuotaStatus(
        limit_reached=limit_reached,
        five_hour=five_hour,
        seven_day=seven_day,
        checked_at=time.monotonic(),
    )


def _fetch_usage(token: str, *, timeout: float = 10.0) -> ClaudeQuotaStatus:
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
        return ClaudeQuotaStatus(checked_at=time.monotonic(), error=str(exc))


def check_claude_quota(
    *,
    creds_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> ClaudeQuotaStatus:
    """Check Claude quota proactively. Returns cached result within TTL.

    Fails open: if auth is missing or API call fails, returns a non-blocking status.
    """
    global _cached_status
    if _cached_status is not None and time.monotonic() - _cached_status.checked_at < cache_ttl:
        return _cached_status

    token = _read_access_token(creds_path)
    if token is None:
        return ClaudeQuotaStatus(checked_at=time.monotonic(), error="no-credentials")

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
        window = "5h" if status.five_hour.used_percent >= 80 else "7d"
        pct = status.five_hour.used_percent if window == "5h" else status.seven_day.used_percent
        reset = status.five_hour.reset_at if window == "5h" else status.seven_day.reset_at
        return f"claude usage limit reached ({window} window at {pct:.0f}%, resets {reset})"
    return None


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
