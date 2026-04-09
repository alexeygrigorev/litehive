"""Shared types, constants, and classifiers used across engine adapters."""

import json
import re
from dataclasses import dataclass


class EngineError(RuntimeError):
    """Raised when an engine cannot be resolved or executed."""


_ENGINE_LIMIT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("hit your usage limit", "usage limit reached"),
    ("usage limit", "usage limit reached"),
    ("spend limit", "budget limit reached"),
    ("quota exceeded", "quota exceeded"),
    ("exceeded your current quota", "quota limit reached"),
    ("quota exhausted", "quota limit reached"),
    ("quota limit", "quota limit reached"),
    ("rate_limit_error", "rate limit reached"),
    ("rate limit", "rate limit reached"),
    ("too many requests", "rate limit reached"),
    ("budget", "budget limit reached"),
    ("credit", "credit limit reached"),
    ("insufficient funds", "budget limit reached"),
    ("purchase more credits", "usage limit reached"),
    ("capacity", "capacity limit reached"),
)

_RETRYABLE_EXECUTION_PATTERNS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "timeout",
        (
            "timed out",
            "timeout",
            "deadline exceeded",
            "etimedout",
            "operation timed out",
            "request timed out",
            "upstream request timeout",
            "read timeout",
            "connect timeout",
            "request_timeout",
            "request timeout",
        ),
        "transient timeout",
    ),
    (
        "network",
        (
            "connection reset",
            "connection refused",
            "network error",
            "temporary failure in name resolution",
            "network is unreachable",
            "socket hang up",
            "econnreset",
            "econnrefused",
            "eai_again",
            "enotfound",
            "broken pipe",
            "connection closed",
            "connection aborted",
            "connection interrupted",
            "error sending request",
            "error trying to connect",
            "peer closed connection",
            "tls handshake eof",
            "socket disconnected before secure tls connection was established",
            "unexpected eof",
            "client network socket disconnected",
            "connect econnrefused",
            "connect enetunreach",
            "connect ehostunreach",
            "write epipe",
            "getaddrinfo eai_again",
            "getaddrinfo enotfound",
            "network connection was lost",
            "connection has been closed",
        ),
        "transient network failure",
    ),
    (
        "service",
        (
            "internal server error",
            "bad gateway",
            "service unavailable",
            "service temporarily unavailable",
            "temporarily unavailable",
            "gateway timeout",
            "server overloaded",
            "overloaded",
            "try again later",
            "server error",
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
            "status code 502",
            "status code 503",
            "status code 504",
            "status: 500",
            "status: 502",
            "status: 503",
            "status: 504",
            "backend error",
            "backend unavailable",
            "api_error",
            "overloaded_error",
            "529",
            "anthropic's systems are overloaded",
        ),
        "transient service failure",
    ),
)

_EXECUTION_INTERRUPTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("keyboardinterrupt", "execution interrupted"),
    ("interrupt signal", "execution interrupted"),
    ("received sigint", "execution interrupted"),
    ("received signal sigint", "execution interrupted"),
    ("received signal 2", "execution interrupted"),
    ("terminated by sigint", "execution interrupted"),
    ("terminated by signal 2", "execution interrupted"),
    ("cancelled by user", "execution interrupted"),
    ("canceled by user", "execution interrupted"),
    ("interrupted by user", "execution interrupted"),
    ("execution interrupted", "execution interrupted"),
)


@dataclass(frozen=True, slots=True)
class RetryableExecutionFailure:
    classification: str
    reason: str


def classify_execution_limit(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    for needle, reason in _ENGINE_LIMIT_PATTERNS:
        if needle in normalized:
            return reason
    return None


def classify_execution_interruption(text: str, *, exit_code: int | None = None) -> str | None:
    if exit_code in {130, 131, 143}:
        return "execution interrupted"
    if exit_code is not None and exit_code < 0 and abs(exit_code) in {2, 15}:
        return "execution interrupted"
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    if not normalized:
        return None
    for needle, reason in _EXECUTION_INTERRUPTION_PATTERNS:
        if needle in normalized:
            return reason
    return None


def classify_retryable_execution_failure(text: str) -> RetryableExecutionFailure | None:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    if (
        not normalized
        or classify_execution_limit(normalized) is not None
        or classify_execution_interruption(normalized) is not None
    ):
        return None
    for classification, needles, reason in _RETRYABLE_EXECUTION_PATTERNS:
        if any(needle in normalized for needle in needles):
            return RetryableExecutionFailure(classification=classification, reason=reason)
    return None


def _decode_json_object(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, str):
        return raw if isinstance(raw, dict) else None
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None
