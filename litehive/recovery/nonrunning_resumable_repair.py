"""Repair pass for non-running but wedged tasks.

Handles tasks that are *not* currently flagged ``running`` but were
left in a wedged shape after a crash (queued/in-progress/interrupted at
a resumable stage); the running-task counterpart lives in
``running_task_recovery``.
"""

import sqlite3
from typing import TypedDict

from litehive.domain.common import RuntimeStageStatus, TaskExecutionStatus, TaskStatus
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.workspace import Workspace


class NonrunningResumableRepairResult(TypedDict):
    """Repair summary returned by :func:`normalize_nonrunning_resumable_tasks` so the caller can persist transitions and journal entries without a second walk."""

    mutated: bool
    transitioned: list[TaskRecord]
    journal_messages: dict[str, str]


def normalize_nonrunning_resumable_tasks(
    state,
    tasks_by_id: dict[str, TaskRecord],
    summary: WorkspaceRepairSummary | None,
) -> NonrunningResumableRepairResult:
    """
    Second-pass repair for tasks left in a wedged shape after a crash.

    Catches queued/in-progress tasks with stale stage status and
    re-canonicalises them so a normal dequeue can pick them up;
    preserves the original queue position when one existed, otherwise
    inserts at the front so previously-active work resumes before
    fresh backlog starts.
    """
    # inline: tasks.queue top-level-imports execution_recovery (would cycle).
    from litehive.tasks.queue import (  # noqa: PLC0415
        canonicalize_resumable_queue_task,
        is_task_eligible_for_execution,
        resumable_queue_stage,
        task_has_resume_marker,
    )

    mutated = False
    transitioned: list[TaskRecord] = []
    journal_messages: dict[str, str] = {}
    front_insertions = 0
    for task in tasks_by_id.values():
        if task.runtime.pipeline.execution_status == TaskExecutionStatus.RUNNING:
            continue
        if task.status not in {TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.INTERRUPTED}:
            continue
        has_resume_marker = task_has_resume_marker(task)
        # Crash cleanup can leave a task queued with execution_status
        # "interrupted" and a trusted stage marker. That state is not eligible
        # for normal dequeue, but repair must still canonicalize it back to a
        # runnable queued/idle task.
        if (
            task.status != TaskStatus.INTERRUPTED
            and not is_task_eligible_for_execution(task)
            and not has_resume_marker
        ):
            continue
        if task.status == TaskStatus.QUEUED and task.id != state.active_task_id and not has_resume_marker:
            continue
        stage = resumable_queue_stage(task)
        if stage is None:
            continue
        queue_contains_task = task.id in state.queue
        if not queue_contains_task:
            queue_index = None
        else:
            queue_index = state.queue.index(task.id)
        should_normalize = (
            task.status != TaskStatus.QUEUED
            or task.runtime.pipeline.execution_status != TaskExecutionStatus.IDLE
            or task.pipeline_status != stage
            or task.runtime.pipeline.current_stage.stage != stage
            or task.runtime.pipeline.current_stage.status != RuntimeStageStatus.IDLE
            or task.id == state.active_task_id
            or not queue_contains_task
        )
        if not should_normalize:
            continue

        was_in_progress = task.status == TaskStatus.IN_PROGRESS
        normalized_stage = canonicalize_resumable_queue_task(task, stage=stage)
        if normalized_stage is None:
            continue

        if queue_contains_task:
            state.queue = [queued_id for queued_id in state.queue if queued_id != task.id]
        if task.id == state.active_task_id or was_in_progress or queue_index is None:
            state.queue.insert(front_insertions, task.id)
            front_insertions += 1
        elif queue_index is not None:
            state.queue.insert(min(queue_index, len(state.queue)), task.id)
        elif task.id not in state.queue:
            state.queue.append(task.id)

        transitioned.append(task)
        mutated = True
        journal_messages[task.id] = f"Recovered stale resumable state and returned the task to `{normalized_stage}`."
        if summary is not None and task.id not in summary.requeued_task_ids:
            summary.requeued_task_ids.append(task.id)

    return {
        "mutated": mutated,
        "transitioned": transitioned,
        "journal_messages": journal_messages,
    }


def has_nonrunning_resumable_repair_candidates(workspace: Workspace) -> bool:
    """
    SQLite-side existence probe for the recovery fast-path skip check.

    Encodes the same "is this task wedged at a resumable stage?"
    predicate as ``normalize_nonrunning_resumable_tasks`` so the
    skip path doesn't have to load every task into Python just to
    learn there's nothing to repair; keeping the predicates in lockstep
    is what stops the fast-path from skipping legitimate work.
    """
    with workspace.connect() as connection:
        try:
            row = connection.execute(
                """
                SELECT 1
                FROM task_state
                WHERE (
                    json_extract(payload, '$.status') = 'in_progress'
                    AND COALESCE(json_extract(payload, '$.runtime.pipeline.execution_status'), 'idle') != 'running'
                    AND (
                        json_extract(payload, '$.pipeline_status')
                            IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'flagged')
                        OR json_extract(payload, '$.runtime.pipeline.current_stage.stage')
                            IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'failed')
                        OR json_extract(payload, '$.runtime.execution.interruption.resume_stage')
                            IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'failed')
                        OR json_extract(payload, '$.runtime.execution.interruption.pipeline_status')
                            IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'flagged')
                    )
                ) OR (
                    json_extract(payload, '$.status') = 'interrupted'
                    AND (
                        json_extract(payload, '$.pipeline_status')
                            IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'flagged')
                        OR json_extract(payload, '$.runtime.execution.interruption.resume_stage')
                            IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'failed')
                        OR json_extract(payload, '$.runtime.execution.interruption.pipeline_status')
                            IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'flagged')
                    )
                ) OR (
                    json_extract(payload, '$.status') = 'queued'
                    AND (
                        (
                            json_extract(payload, '$.runtime.pipeline.current_stage.stage')
                                IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'failed')
                            AND json_extract(payload, '$.runtime.pipeline.current_stage.status')
                                IN ('idle', 'paused', 'interrupted', 'running')
                        )
                        OR json_extract(payload, '$.runtime.execution.interruption.resume_stage')
                            IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'failed')
                        OR json_extract(payload, '$.runtime.execution.interruption.pipeline_status')
                            IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'flagged')
                    )
                )
                LIMIT 1
                """
            ).fetchone()
        except sqlite3.OperationalError:
            return False
    return row is not None
