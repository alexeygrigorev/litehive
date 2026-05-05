"""Dataclasses and error types for the tasks package."""

from dataclasses import dataclass, field
import threading
from typing import TextIO

from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import TaskRecord


@dataclass(slots=True)
class RunnerLockState:
    """Thread-safe state for the runner process lock.

    Tracks the lock file handle, recursion depth, and current runner status.
    Used by TaskRunner to coordinate exclusive workspace access and prevent
    concurrent task execution conflicts.
    """

    handle: TextIO  # Open file handle to the lock file
    depth: int  # Recursion depth for reentrant locking
    status: RunnerStatusState  # Current runner execution status
    owner_thread_id: int = 0  # Thread that owns the lock
    metadata_lock: threading.Lock = field(default_factory=threading.Lock)  # Protects status updates


@dataclass(slots=True)
class BlockedTask:
    """Summary of a task that cannot proceed due to dependencies.

    Used by task selection and queue management to track tasks waiting
    on upstream dependencies. Helps operators understand task ordering
    and dependency bottlenecks.
    """

    task_id: str  # Unique task identifier
    title: str  # Brief task summary
    queue_position: int  # Position in the task queue
    blocked_by: list[str]  # Task IDs this task depends on


@dataclass(slots=True)
class TaskSelection:
    """Result of selecting the next task for execution.

    Contains either a ready-to-run task or information about why no task
    could be selected. Used by TaskRunner to determine what work to do
    next and provide feedback about queue bottlenecks.
    """

    task: TaskRecord | None  # Selected task ready for execution, if any
    blocked: list[BlockedTask]  # Tasks that cannot run due to dependencies


@dataclass(slots=True)
class TaskPlan:
    """Complete view of all tasks in dependency order.

    Shows both executable and blocked tasks to help operators understand
    the complete task queue state and dependency relationships.
    Used by CLI commands for displaying task status and queue planning.
    """

    tasks: list[TaskRecord]  # All tasks in dependency-respecting order
    blocked: list[BlockedTask]  # Tasks blocked by dependencies


@dataclass(slots=True)
class WorkspaceRepairSummary:
    """Summary of workspace repair operations performed.

    Records what cleanup and recovery actions were taken to restore
    workspace consistency. Used by health check commands and recovery
    logic to report what repairs were needed and applied.
    """

    mutated: bool = False  # Whether any workspace state was changed
    stale_runner_recovered: bool = False  # Whether a stale runner was cleaned up
    cleared_active_task_id: str | None = None  # Active task cleared during recovery
    requeued_task_ids: list[str] = field(default_factory=list)  # Tasks moved back to queue
    stale_process_task_ids: list[str] = field(default_factory=list)  # Tasks with stale process state
    terminal_task_ids: list[str] = field(default_factory=list)  # Tasks normalized back to terminal state

    @property
    def repaired(self) -> bool:
        """Alias for ``mutated`` used by the operator-facing repair summary; reads more naturally in the CLI ``repaired: yes/no`` output line."""
        return self.mutated


class WorkspaceConflictError(ValueError):
    """Raised when workspace mutations would conflict with an active runner."""


@dataclass(slots=True)
class StopTaskSummary:
    """Summary of a task stop operation.

    Records which task was stopped and what process was terminated.
    Used by task control commands to report the outcome of stop requests
    and provide visibility into task lifecycle management.
    """

    task: TaskRecord  # Task that was stopped
    runner_pid: int | None = None  # Process ID of the runner that was stopped
    signal_sent: bool = False


@dataclass(slots=True)
class SwitchTaskSummary:
    """Summary of a task engine switch operation.

    Returned by switch_task_engine() to communicate the result of switching
    a task from one AI engine to another. Tracks the state transition details
    including whether the task was actively running and needed to be stopped.
    Primary consumers: CLI switch commands and engine routing diagnostics.
    """

    task: TaskRecord  # Task that had its engine switched
    previous_engine: str  # Engine the task was using before
    new_engine: str  # Engine the task was switched to
    was_active: bool = False  # Whether the task was actively running during switch
    runner_pid: int | None = None  # Process ID of active runner if it was stopped
    signal_sent: bool = False  # Whether a termination signal was sent to the runner
    prior_work_paths: list[str] = field(default_factory=list)  # Paths to prior work in the old engine context
