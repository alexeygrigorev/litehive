"""Shared constants for the tasks package."""

import threading
from pathlib import Path


VALID_TASK_PRIORITIES = {"low", "medium", "high", "critical"}
VALID_TASK_ENGINES = {"codex", "opencode", "gemini", "copilot", "claude", "goz"}
VALID_TASK_TYPES = {"adapter", "bugfix", "docs", "refactor", "research", "review"}
TASK_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

MISSING = object()
# A live runner that hasn't refreshed its heartbeat in this many seconds is considered late.
HEARTBEAT_LATE_THRESHOLD_SECONDS = 60
CLOSED_TASK_STATUSES = {"cancelled", "wont_do", "deferred", "duplicate"}
RESUMABLE_TASK_STATUSES = {"interrupted", "parked"}

RUNNER_LOCKS: dict[Path, "_RunnerLockState"] = {}  # type: ignore[name-defined]
RUNNER_LOCKS_MUTEX = threading.Lock()
