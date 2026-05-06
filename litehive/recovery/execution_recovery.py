"""Execution recovery helpers."""

from pathlib import Path

from litehive.container import build_workspace
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
    current_thread_owns_runner_guard,
    runner_lock_is_held,
    workspace_lock,
)
from litehive.state.persist import (
    load_state as load_workspace_state,
    persist_tasks_and_state_without_runner_guard,
    save_state_without_runner_guard,
)
from litehive.state.records import list_tasks
from litehive.workspace import Workspace


__all__ = [
    "interruption_journal_message",
    "mark_interrupted_subagent",
    "prepare_interrupted_task",
    "recover_stale_runner_state",
    "recover_stale_runner_state_for_workspace",
    "stale_interruption_reason",
]


def recover_stale_runner_state(
    root: Path,
    summary: WorkspaceRepairSummary | None = None,
) -> bool:
    """
    Top-level entry for "is the workspace stuck because a previous runner died?".

    Invoked by the queue, daemon, ``litehive stop``, and the CLI repair
    flows. Returns whether anything was mutated; takes the workspace
    lock and only acts when no live runner owns the runner lock, so a
    live runner cannot be repaired out from under itself.
    """
    return recover_stale_runner_state_for_workspace(build_workspace(root.resolve()), summary=summary)


def recover_stale_runner_state_for_workspace(
    workspace: Workspace,
    summary: WorkspaceRepairSummary | None = None,
) -> bool:
    """
    Workspace-based implementation for stale-runner recovery.

    ``recover_stale_runner_state`` is the public path boundary; callers
    that already have a ``Workspace`` use this helper so recovery does not
    rebuild the workspace dependency graph.
    """
    root = workspace.root
    with workspace_lock(root):
        state = load_workspace_state(root)
        running_task_ids = _running_task_ids(workspace)
        if _can_skip_recovery_scan(
            root,
            state.active_task_id,
            running_task_ids,
            current_thread_owns_runner_guard=current_thread_owns_runner_guard(root),
            runner_lock_held=runner_lock_is_held(root),
            has_repair_candidates=has_nonrunning_resumable_repair_candidates(workspace),
        ):
            return False
        # Repair must tolerate disk-only task dirs that are missing runtime
        # rows so one stale record does not block runner recovery.
        tasks = list_tasks(root, strict=False)
        tasks_by_id = {task.id: task for task in tasks}
        if not can_attempt_stale_runner_recovery(workspace, tasks_by_id, running_task_ids):
            return False

        recovery = recover_running_tasks(
            workspace,
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
                task for task in normalized["transitioned"] if all(existing.id != task.id for existing in transitioned)
            )
            journal_messages.update(normalized["journal_messages"])

        if update_active_task_after_recovery(
            workspace,
            state,
            tasks_by_id=tasks_by_id,
            prioritized_ids=prioritized_ids,
            running_task_ids=running_task_ids,
            summary=summary,
        ):
            mutated = True
        if transitioned:
            persist_tasks_and_state_without_runner_guard(
                root,
                tasks=transitioned,
                state=state,
                journal_messages=journal_messages,
            )
        elif mutated:
            save_state_without_runner_guard(root, state)
        return mutated


def _can_skip_recovery_scan(
    root: Path,
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
    del root
    return (
        not running_task_ids
        and active_task_id is None
        and not current_thread_owns_runner_guard
        and not runner_lock_held
        and not has_repair_candidates
    )
