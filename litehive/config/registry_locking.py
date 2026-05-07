"""
Lock and retry policy for the global workspace registry.

The registry itself owns SQLite schema and workspace-root rows. This
module owns only contention policy: environment tunables, SQLite
timeout values, and the retry loop used when SQLite returns
``locked`` or ``busy`` after its own busy timeout.
"""

from pathlib import Path
from typing import Callable, TypeVar
import os
import sqlite3
import time

_DEFAULT_BUSY_TIMEOUT_MS = 30_000
_DEFAULT_LOCK_RETRIES = 0
_DEFAULT_LOCK_RETRY_DELAY_MS = 100

T = TypeVar("T")


def _int_env(name: str, default: int) -> int:
    """
    Read a non-negative integer from the named env var.

    Falls back to ``default`` for unset or unparseable values.
    Used by the registry tunables so operators and CI can override
    locking behaviour via env without editing code.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def registry_lock_retries() -> int:
    """
    Extra retries the registry tolerates beyond the SQLite busy timeout.

    Tunable via ``LITEHIVE_REGISTRY_LOCK_RETRIES`` so CI can crank it
    up without code changes when concurrent test runs hit the same
    file.
    """
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRIES", _DEFAULT_LOCK_RETRIES)


def registry_busy_timeout_ms() -> int:
    """
    SQLite ``PRAGMA busy_timeout`` value in milliseconds.

    Clamped to at least 1 ms so a misconfigured zero does not disable
    busy waits entirely.
    """
    return max(_int_env("LITEHIVE_REGISTRY_BUSY_TIMEOUT_MS", _DEFAULT_BUSY_TIMEOUT_MS), 1)


def registry_busy_timeout_seconds() -> float:
    """
    Busy timeout in seconds for the ``sqlite3.connect`` ``timeout`` parameter.

    Same value as :func:`registry_busy_timeout_ms`, just rescaled. The
    connect-level timeout uses seconds while the pragma uses
    milliseconds.
    """
    return registry_busy_timeout_ms() / 1000


def registry_lock_retry_delay_seconds() -> float:
    """
    Sleep between retry attempts after SQLite reports lock contention.

    Small by default so the worst case is a brief stall rather than a
    tight CPU spin. Tunable via env so a contention test can pin the
    delay to zero.
    """
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRY_DELAY_MS", _DEFAULT_LOCK_RETRY_DELAY_MS) / 1000


def locked_registry_operation(operation: Callable[[], T], path: Path) -> T:
    """
    Run a registry callable with bounded retries through SQLite locks.

    Retries through ``database is locked``/``busy`` errors up to
    :func:`registry_lock_retries` times before surfacing
    ``TimeoutError``; wraps registry read/write calls so contention
    with another process becomes a bounded wait rather than an
    immediate failure visible to the operator.
    """
    retries_remaining = registry_lock_retries()
    retry_delay_seconds = registry_lock_retry_delay_seconds()
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if retries_remaining <= 0:
                raise TimeoutError(f"workspace registry remained locked: {path}") from None
            retries_remaining -= 1
            if retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)
