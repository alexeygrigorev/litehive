"""Single-cycle task execution for daemon-spawned ``litehive run`` processes."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from litehive.container import LitehiveContainer
from litehive.domain.common import PipelineState
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.lifecycle.orchestration import ExecutionResult
from litehive.state.persist import (
    CONSECUTIVE_TASK_FAILURE_STOP_REASON,
    WorkspaceStateRepository,
)
from litehive.tasks.queue import TaskQueueService
from litehive.workspace import Workspace


@dataclass(slots=True)
class DaemonRunIteration:
    """
    One pass of ``litehive run`` task execution.

    Carries the exit code plus pool-state signals
    (``consecutive_task_failures``, ``pool_stop_reason``) so the
    drain loop and the single-shot path can decide whether to
    continue or stop without re-reading state from the database.
    """

    exit_code: int
    ran_task: bool
    final_stage: PipelineState | None = None
    consecutive_task_failures: int = 0
    pool_stop_reason: str | None = None


TaskPicker = Callable[[Workspace], TaskRecord | None]
LineWriter = Callable[[str], None]


def pick_next_task(workspace: Workspace) -> TaskRecord | None:
    """
    Select the next runnable task through the workspace-bound queue service.
    """
    return TaskQueueService(workspace).dequeue_next()


class TaskRunner(Protocol):
    """
    Callable seam for task orchestration.
    """

    def __call__(
        self,
        container: LitehiveContainer,
        task: TaskRecord,
        engine_override: str | None = None,
        model_override: str | None = None,
    ) -> ExecutionResult:
        """
        Run a selected task with optional engine/model overrides.
        """


@dataclass(slots=True)
class DaemonExecution:
    """
    One ``litehive run`` cycle: choose a queued task, run it, and record its result.

    This is the task-execution half of daemon progress. The outer
    workspace daemon decides when to spawn ``litehive run``; this object
    owns what one spawned run does once it starts.
    """

    container: LitehiveContainer
    engine: str | None = None
    model: str | None = None
    task_runner: TaskRunner | None = None
    task_picker: TaskPicker = pick_next_task
    output: LineWriter = print

    def run_once(self) -> DaemonRunIteration:
        """
        Execute one task if the pool has runnable work.
        """
        stopped_iteration = self.record_cycle_start()
        if stopped_iteration is not None:
            return stopped_iteration

        try:
            task = self.pick_next_task()
        except WorkspaceConflictError as exc:
            self.output(f"run failed: {exc}")
            return DaemonRunIteration(exit_code=1, ran_task=False)
        except Exception as exc:
            self.output(f"run failed: {exc}")
            return DaemonRunIteration(exit_code=1, ran_task=False)

        if task is None:
            return self.record_cycle_finish()

        try:
            result = self.run_task(task)
        except Exception as exc:
            self.output(f"run failed: {exc}")
            return DaemonRunIteration(exit_code=1, ran_task=False)

        return self.handle_result(result)

    def record_cycle_start(self) -> DaemonRunIteration | None:
        """
        Return a synthetic stopped iteration when the pool is already halted.
        """
        state = WorkspaceStateRepository(self.container.workspace).load()
        if state.pool_stop_reason != CONSECUTIVE_TASK_FAILURE_STOP_REASON:
            return None
        self.emit_consecutive_task_failure_stop(state.consecutive_task_failures)
        return DaemonRunIteration(
            exit_code=0,
            ran_task=False,
            consecutive_task_failures=state.consecutive_task_failures,
            pool_stop_reason=state.pool_stop_reason,
        )

    def pick_next_task(self) -> TaskRecord | None:
        """
        Dequeue the next runnable task for this workspace.
        """
        return self.task_picker(self.container.workspace)

    def run_task(self, task: TaskRecord) -> ExecutionResult:
        """
        Invoke task orchestration for the selected task.
        """
        if self.task_runner is None:
            raise RuntimeError("task_runner is required")
        return self.task_runner(
            self.container,
            task,
            engine_override=self.engine,
            model_override=self.model,
        )

    def handle_result(self, result: ExecutionResult) -> DaemonRunIteration:
        """
        Print the task result and record pool-completion counters.
        """
        if result.task is not None:
            self.output(f"task: {result.task.id} {result.task.title}")
        self.output(f"final_stage: {result.final_stage}")
        if result.failed_reason:
            self.output(f"failed_reason: {result.failed_reason}")
        if result.failed_message:
            self.output(f"failed_message: {result.failed_message}")
        return self.record_cycle_finish(result)

    def record_cycle_finish(self, result: ExecutionResult | None = None) -> DaemonRunIteration:
        """
        Convert the end of a cycle into the caller-facing iteration result.
        """
        workspace = self.container.workspace
        if result is None:
            state = WorkspaceStateRepository(workspace).load()
            return DaemonRunIteration(
                exit_code=0,
                ran_task=False,
                consecutive_task_failures=state.consecutive_task_failures,
                pool_stop_reason=state.pool_stop_reason,
            )

        consecutive_task_failures, pool_stop_reason = WorkspaceStateRepository(workspace).record_task_completion(
            final_stage=result.final_stage
        )
        if pool_stop_reason == CONSECUTIVE_TASK_FAILURE_STOP_REASON:
            self.emit_consecutive_task_failure_stop(consecutive_task_failures)
        return DaemonRunIteration(
            exit_code=0,
            ran_task=True,
            final_stage=result.final_stage,
            consecutive_task_failures=consecutive_task_failures,
            pool_stop_reason=pool_stop_reason,
        )

    def emit_consecutive_task_failure_stop(self, consecutive_task_failures: int) -> None:
        """
        Print the operator-visible banner when the pool stops after repeated failures.
        """
        failure_count = max(3, int(consecutive_task_failures))
        self.output(f"critical_status: stopped after {failure_count} consecutive task failures")
