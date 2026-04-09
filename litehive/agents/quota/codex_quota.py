"""Proactive codex quota checking via chatgpt.com API."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_AUTH_PATH = Path.home() / ".codex" / "auth.json"
_CACHE_TTL_SECONDS = 60


@dataclass(slots=True)
class CodexQuotaWindow:
    used_percent: float = 0.0
    reset_at: str | None = None


@dataclass(slots=True)
class CodexQuotaStatus:
    limit_reached: bool = False
    primary_window: CodexQuotaWindow = field(default_factory=CodexQuotaWindow)
    secondary_window: CodexQuotaWindow = field(default_factory=CodexQuotaWindow)
    checked_at: float = 0.0
    error: str | None = None

    @property
    def earliest_reset_at(self) -> str | None:
        if self.primary_window.reset_at and self.secondary_window.reset_at:
            return max(self.primary_window.reset_at, self.secondary_window.reset_at)
        return self.primary_window.reset_at or self.secondary_window.reset_at

    @property
    def max_used_percent(self) -> float:
        return max(self.primary_window.used_percent, self.secondary_window.used_percent)


# Module-level cache
_cached_status: CodexQuotaStatus | None = None


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


def _parse_quota_response(data: dict) -> CodexQuotaStatus:
    rate_limit = data.get("rate_limit", {})
    limit_reached = bool(rate_limit.get("limit_reached", False))

    primary_data = rate_limit.get("primary_window", {})
    secondary_data = rate_limit.get("secondary_window", {})

    primary = CodexQuotaWindow(
        used_percent=float(primary_data.get("used_percent", 0)),
        reset_at=str(primary_data["reset_at"]) if "reset_at" in primary_data else None,
    )
    secondary = CodexQuotaWindow(
        used_percent=float(secondary_data.get("used_percent", 0)),
        reset_at=str(secondary_data["reset_at"]) if "reset_at" in secondary_data else None,
    )

    return CodexQuotaStatus(
        limit_reached=limit_reached,
        primary_window=primary,
        secondary_window=secondary,
        checked_at=time.monotonic(),
    )


def _fetch_quota(token: str, *, timeout: float = 10.0) -> CodexQuotaStatus:
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
        return CodexQuotaStatus(checked_at=time.monotonic(), error=str(exc))


def check_codex_quota(
    *,
    auth_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> CodexQuotaStatus:
    """Check codex quota proactively. Returns cached result within TTL.

    Fails open: if auth is missing or API call fails, returns a non-blocking status.
    """
    global _cached_status
    now = time.monotonic()

    if _cached_status is not None and (now - _cached_status.checked_at) < cache_ttl:
        return _cached_status

    token = _read_bearer_token(auth_path)
    if token is None:
        status = CodexQuotaStatus(checked_at=now, error="no auth token")
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
        reset_info = f", resets at {status.earliest_reset_at}" if status.earliest_reset_at else ""
        return f"codex quota exhausted (used {status.max_used_percent:.0f}%{reset_info})"
    return None


def reset_cache() -> None:
    """Clear the cached quota status (useful for testing)."""
    global _cached_status
    _cached_status = None
