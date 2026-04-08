"""Shared constants for the tasks package."""

import threading
from pathlib import Path
from typing import get_args

from litehive.models import PlannedEffort, TaskComplexity

VALID_TASK_PRIORITIES = {"low", "medium", "high", "critical"}
VALID_TASK_ENGINES = {"codex", "opencode", "gemini", "copilot", "claude", "goz"}
VALID_HUMAN_CHECKPOINTS = {"before_acceptance", "before_commit"}
VALID_PM_COMPLEXITIES = set(get_args(TaskComplexity))
VALID_PLANNED_EFFORTS = set(get_args(PlannedEffort))
VALID_TASK_TYPES = {"adapter", "bugfix", "docs", "intake", "refactor", "research", "review"}
TASK_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

_MISSING = object()
HEARTBEAT_LATE_THRESHOLD_SECONDS = 60
CLOSED_TASK_STATUSES = {"cancelled", "wont_do", "deferred", "duplicate"}
RESUMABLE_TASK_STATUSES = {"interrupted", "parked"}

_RUNNER_LOCKS: dict[Path, "object"] = {}  # populated with _RunnerLockState at runtime
_RUNNER_LOCKS_MUTEX = threading.Lock()
