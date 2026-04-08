"""Dataclass models for the tasks package."""

from dataclasses import dataclass, field
import threading
from typing import TextIO

from litehive.models import (
    RunnerStatusState,
    TaskRecord,
)


@dataclass(slots=True)
class _RunnerLockState:
    handle: TextIO
    depth: int
    status: RunnerStatusState
    owner_thread_id: int = 0
    metadata_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(slots=True)
class BlockedTask:
    task_id: str
    title: str
    queue_position: int
    blocked_by: list[str]


@dataclass(slots=True)
class TaskSelection:
    task: TaskRecord | None
    blocked: list[BlockedTask]


@dataclass(slots=True)
class TaskPlan:
    tasks: list[TaskRecord]
    blocked: list[BlockedTask]


@dataclass(slots=True)
class WorkspaceRepairSummary:
    mutated: bool = False
    stale_runner_recovered: bool = False
    cleared_active_task_id: str | None = None
    requeued_task_ids: list[str] = field(default_factory=list)
    removed_queue_entries: list[str] = field(default_factory=list)
    deduped_queue_entries: list[str] = field(default_factory=list)
    restored_queue_entries: list[str] = field(default_factory=list)
    finalized_commit_task_ids: list[str] = field(default_factory=list)
    stale_process_task_ids: list[str] = field(default_factory=list)
    inactivity_recovered_task_ids: list[str] = field(default_factory=list)


class WorkspaceConflictError(ValueError):
    """Raised when workspace mutations would conflict with an active runner."""


@dataclass(slots=True)
class StopTaskSummary:
    task: TaskRecord
    runner_pid: int | None = None
    signal_sent: bool = False


@dataclass(slots=True)
class SwitchTaskSummary:
    task: TaskRecord
    previous_engine: str
    new_engine: str
    was_active: bool = False
    runner_pid: int | None = None
    signal_sent: bool = False
    prior_work_paths: list[str] = field(default_factory=list)
