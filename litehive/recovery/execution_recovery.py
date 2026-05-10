"""Execution recovery helpers."""

from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.recovery.interrupted_subagent import mark_interrupted_subagent
from litehive.recovery.interruption_state import (
    interruption_journal_message,
    prepare_interrupted_task,
    stale_interruption_reason,
)
from litehive.recovery.nonrunning_resumable_repair import (
    has_nonrunning_resumable_repair_candidates,
    normalize_nonrunning_resumable_tasks,
)
from litehive.recovery.running_task_recovery import (
    can_attempt_stale_runner_recovery,
    recover_running_tasks,
    running_task_ids as _running_task_ids,
    update_active_task_after_recovery,
)
from litehive.state.locking import (
    WorkspaceRunnerLock,
    WorkspaceStateLock,
)
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace


__all__ = [
    "RunnerRecoveryService",
    "interruption_journal_message",
    "mark_interrupted_subagent",
    "prepare_interrupted_task",
    "stale_interruption_reason",
]


class RunnerRecoveryService:
    """
    Workspace-bound stale runner recovery service.

    Owns crash recovery for active/running task state. Binding the
    workspace once keeps queue, stop, and repair callers from threading
    recovery state through a free helper.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Bind the service to a single workspace for its entire lifetime.

        The workspace reference is what lets queue, stop, and repair callers
        share one recovery instance instead of threading the workspace
        through free helper signatures.
        """
        self.workspace = workspace

    def recover_stale_runner_state(self, summary: WorkspaceRepairSummary | None = None) -> bool:
        """
        Recover stale runner state for the bound workspace.

        Returns whether anything was mutated; takes the workspace lock and
        only acts when no live runner owns the runner lock, so a live runner
        cannot be repaired out from under itself.
        """
        with WorkspaceStateLock(self.workspace).hold():
            state = WorkspaceStateRepository(self.workspace).load()
            running_task_ids = _running_task_ids(self.workspace)
            if _can_skip_recovery_scan(
                state.active_task_id,
                running_task_ids,
                current_thread_owns_runner_guard=WorkspaceRunnerLock(self.workspace).owns_current_thread(),
                runner_lock_held=WorkspaceRunnerLock(self.workspace).is_held(),
                has_repair_candidates=has_nonrunning_resumable_repair_candidates(self.workspace),
            ):
                return False
            # Repair must tolerate disk-only task dirs that are missing runtime
            # rows so one stale record does not block runner recovery.
            tasks = WorkspaceTasks(self.workspace).list(strict=False)
            tasks_by_id = {task.id: task for task in tasks}
            if not can_attempt_stale_runner_recovery(self.workspace, tasks_by_id, running_task_ids):
                return False

            recovery = recover_running_tasks(
                self.workspace,
                state,
                tasks_by_id,
                running_task_ids,
                summary=summary,
            )
            mutated = recovery["mutated"]
            transitioned = recovery["transitioned"]
            prioritized_ids = recovery["prioritized_ids"]
            journal_messages = recovery["journal_messages"]

            normalized = normalize_nonrunning_resumable_tasks(
                state,
                tasks_by_id=tasks_by_id,
                summary=summary,
            )
            if normalized["mutated"]:
                mutated = True
                transitioned.extend(
                    task
                    for task in normalized["transitioned"]
                    if all(existing.id != task.id for existing in transitioned)
                )
                journal_messages.update(normalized["journal_messages"])

            if update_active_task_after_recovery(
                self.workspace,
                state,
                tasks_by_id=tasks_by_id,
                prioritized_ids=prioritized_ids,
                running_task_ids=running_task_ids,
                summary=summary,
            ):
                mutated = True
            if transitioned:
                WorkspaceStateRepository(self.workspace).persist_tasks_and_state_without_runner_guard(
                    tasks=transitioned,
                    state=state,
                    journal_messages=journal_messages,
                )
            elif mutated:
                WorkspaceStateRepository(self.workspace).save_without_runner_guard(state)
            return mutated


def _can_skip_recovery_scan(
    active_task_id: str | None,
    running_task_ids: list[str],
    current_thread_owns_runner_guard: bool,
    runner_lock_held: bool,
    has_repair_candidates: bool,
) -> bool:
    """
    Cheap fast-path that bypasses the full repair scan on a quiet workspace.

    Protects the hot start-of-runner path from doing expensive SQL work
    on every launch; the conjunction (no running tasks, no active
    pointer, no repair candidates, no held lock) is the unambiguous
    "nothing to do" shape so skipping it is safe.
    """
    return (
        not running_task_ids
        and active_task_id is None
        and not current_thread_owns_runner_guard
        and not runner_lock_held
        and not has_repair_candidates
    )
