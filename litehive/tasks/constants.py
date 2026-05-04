"""Shared constants for the tasks package."""

import threading
from pathlib import Path

from litehive.domain.common import TaskStatus
from litehive.domain.task_ops import RunnerLockState


VALID_TASK_PRIORITIES = {"low", "medium", "high", "critical"}
VALID_TASK_ENGINES = {"codex", "opencode", "gemini", "copilot", "claude", "goz"}
TASK_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

MISSING = object()
# A live runner that hasn't refreshed its heartbeat in this many seconds is considered late.
HEARTBEAT_LATE_THRESHOLD_SECONDS = 60
CLOSED_TASK_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.CLOSED})
RESUMABLE_TASK_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.INTERRUPTED, TaskStatus.PARKED})

RUNNER_LOCKS: dict[Path, RunnerLockState] = {}
RUNNER_LOCKS_MUTEX = threading.Lock()
