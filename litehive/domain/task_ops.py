"""
Operation-result dataclasses and error types for task-management flows.

These are the shapes the task service returns (selection, repair, stop,
switch summaries) plus the lock state the runner uses to coordinate
exclusive workspace access. Keeping them in ``domain`` lets the CLI
render results without importing the task service implementation, and
lets tests construct them directly.
"""

from dataclasses import dataclass, field
import threading
from typing import TextIO

from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import TaskRecord


@dataclass(slots=True)
class RunnerLockState:
    """
    Thread-safe handle for the runner's exclusive workspace lock.

    Tracks the open lockfile, the reentrant depth count (so nested
    runner calls don't double-release), and the live
    ``RunnerStatusState`` whose updates ``metadata_lock`` serializes.
    Owned by ``TaskRunner`` to prevent two concurrent task executions
    from corrupting workspace state.
    """

    handle: TextIO  # Open file handle to the lock file
    depth: int  # Recursion depth for reentrant locking
    status: RunnerStatusState  # Current runner execution status
    owner_thread_id: int = 0  # Thread that owns the lock
    metadata_lock: threading.Lock = field(default_factory=threading.Lock)  # Protects status updates


@dataclass(slots=True)
class BlockedTask:
    """
    Summary row for a task that the queue can't pick because deps aren't done.

    The selection helper returns these alongside the chosen task so
    the CLI can render "X is queued at position 3 but is waiting on
    Y" — without that context an operator would only see "no task
    selected" and have to grep dependency lists themselves.
    """

    task_id: str  # Unique task identifier
    title: str  # Brief task summary
    queue_position: int  # Position in the task queue
    blocked_by: list[str]  # Task IDs this task depends on


@dataclass(slots=True)
class TaskSelection:
    """
    Output of "pick the next task to run".

    Carries either the chosen ``TaskRecord`` plus an empty
    ``blocked`` list, or ``task=None`` plus the list of dependency-
    blocked tasks the selector skipped. ``TaskRunner`` and the CLI
    consume the same shape so a successful pick and a "nothing to do
    but here's why" branch render through one path.
    """

    task: TaskRecord | None  # Selected task ready for execution, if any
    blocked: list[BlockedTask]  # Tasks that cannot run due to dependencies


@dataclass(slots=True)
class WorkspaceRepairSummary:
    """
    Per-invocation report from the workspace-repair flow.

    The repair command (and recovery's auto-repair entrypoint) walks
    each consistency check and appends to the lists below. ``mutated``
    is the binary "did we change anything?" signal; the typed lists
    let the operator output enumerate exactly which tasks/runner state
    were touched. Empty summary = workspace was already healthy.
    """

    mutated: bool = False  # Whether any workspace state was changed
    stale_runner_recovered: bool = False  # Whether a stale runner was cleaned up
    cleared_active_task_id: str | None = None  # Active task cleared during recovery
    requeued_task_ids: list[str] = field(default_factory=list)  # Tasks moved back to queue
    stale_process_task_ids: list[str] = field(default_factory=list)  # Tasks with stale process state
    terminal_task_ids: list[str] = field(default_factory=list)  # Tasks normalized back to terminal state

    @property
    def repaired(self) -> bool:
        """
        Alias for ``mutated`` reading more naturally in the CLI summary.

        The operator-facing line is ``repaired: yes/no``, which is
        easier to scan than ``mutated: true/false``. Both names point
        at the same flag — having only ``mutated`` would force the
        CLI to do its own inversion.
        """
        return self.mutated


class WorkspaceConflictError(ValueError):
    """
    Raised when a workspace mutation would race a live runner.

    The ``persist_*`` helpers raise this to refuse a write that would
    clobber state another runner is in the middle of updating;
    callers must back off and retry rather than override. A
    ``ValueError`` subclass so existing ``except ValueError`` blocks
    still catch it, but distinct enough that conflict-aware callers
    can route on the specific class.
    """


@dataclass(slots=True)
class StopTaskSummary:
    """
    Outcome record returned by ``stop_task``.

    Distinguishes "stopped a queued task" (no runner, no signal) from
    "stopped a running task" (``runner_pid`` set, ``signal_sent``
    true) so the CLI can render the right operator message and tests
    can assert on the exact path taken without reaching into the
    runner state.
    """

    task: TaskRecord  # Task that was stopped
    runner_pid: int | None = None  # Process ID of the runner that was stopped
    signal_sent: bool = False


@dataclass(slots=True)
class SwitchTaskSummary:
    """
    Outcome record returned by ``switch_task_engine``.

    Carries before/after engine names plus the runner-side state
    transition (was the task running, did we have to signal a runner,
    what prior-work paths exist) so the CLI can explain to the
    operator what happened and engine-routing diagnostics can audit
    the switch trail.
    """

    task: TaskRecord  # Task that had its engine switched
    previous_engine: str  # Engine the task was using before
    new_engine: str  # Engine the task was switched to
    was_active: bool = False  # Whether the task was actively running during switch
    runner_pid: int | None = None  # Process ID of active runner if it was stopped
    signal_sent: bool = False  # Whether a termination signal was sent to the runner
    prior_work_paths: list[str] = field(default_factory=list)  # Paths to prior work in the old engine context
