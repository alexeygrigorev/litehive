"""Execution recovery helpers."""

from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from litehive.agents.execution_trace import load_subagent_execution_trace
from litehive.agents.session_store import (
    load_subagent_report,
    load_subagent_session,
    save_subagent_artifacts,
)
from litehive.config.loading import load_config
from litehive.domain.common import utcnow
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.reports import RecoveryAction
from litehive.domain.runtime import (
    RuntimeInterruptionState,
    RuntimeSubagentState,
)
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.db.schema import connect_workspace_db
from litehive.observability.events import last_event_timestamp
from litehive.state.locking import (
    current_thread_owns_runner_guard,
    read_runner_lock_metadata,
    runner_lock_is_held,
    runner_lock_pid_is_stale,
    runner_metadata_present,
    subagent_process_is_stale,
    workspace_lock,
)
from litehive.state.persist import (
    load_state as load_workspace_state,
    persist_tasks_and_state_without_runner_guard,
    save_state_without_runner_guard,
)
from litehive.state.records import list_tasks
from litehive.tasks.recovery_reports import record_recovery_report
from litehive.tasks.runtime import (
    apply_task_outcome,
    duration_seconds,
    summarize_transcript,
)


def mark_interrupted_subagent(root: Path, task: TaskRecord, *, reason: str, stage: str) -> RuntimeSubagentState | None:
    active = task.runtime.execution.active_subagent
    interruption = task.runtime.execution.interruption
    existing = None if interruption is None else interruption.subagent
    if active is None and (existing is None or existing.status != "interrupted"):
        return None
    now = utcnow()
    source = active or existing
    assert source is not None
    if active is not None:
        for ref in reversed(task.subagents):
            if ref.id == active.id and ref.status == "running":
                ref.status = "interrupted"
                break
    snippet = source.execution_trace_snippet
    if active is not None or not snippet:
        snippet = _interrupted_subagent_snippet(root, task, source)
    interrupted = source.model_copy(
        update={
            "status": "interrupted",
            "updated_at": now,
            "completed_at": source.completed_at or now,
            "execution_trace_snippet": snippet,
            "interruption_reason": _interrupted_subagent_reason(task, reason),
        }
    )
    task.runtime.execution.active_subagent = None
    _write_interrupted_subagent_artifacts(root, task, interrupted, resume_stage=stage)
    return interrupted


def prepare_interrupted_task(
    root: Path,
    task: TaskRecord,
    *,
    stage: str,
    summary: str,
    reason: str | None = None,
) -> None:
    now = utcnow()
    interruption_reason = reason or summary
    timestamps = _interruption_timestamps(task, now)
    task.status = "interrupted"
    task.pipeline_status = stage
    task.runtime.pipeline.execution_status = "interrupted"
    task.runtime.pipeline.run_started_at = None
    task.runtime.pipeline.updated_at = now
    _set_interruption_metadata(
        task,
        root=root,
        stage=stage,
        summary=summary,
        reason=interruption_reason,
        now=now,
        started_at=timestamps["started_at"],
        run_started_at=timestamps["run_started_at"],
        stage_started_at=timestamps["stage_started_at"],
        interrupted_at=timestamps["interrupted_at"],
    )


def interruption_journal_message(task: TaskRecord) -> str:
    interruption = task.runtime.execution.interruption
    if interruption is None:
        return f"Interrupted run recorded. Resume from `{task.pipeline_status}`."
    parts = [
        (
            f"Interrupted {interruption.source} execution while "
            f"`{interruption.stage or task.pipeline_status}` was running."
        ),
        f"Reason: {interruption.reason or interruption.summary or 'unknown'}.",
    ]
    if interruption.subagent is not None:
        subagent = interruption.subagent
        pid = subagent.pid if subagent.pid is not None else "-"
        parts.append(
            f"Subagent `{subagent.id}` ({subagent.role}/{subagent.engine}, pid={pid}, "
            f"path `{subagent.path}`) stopped with status `{subagent.status}`."
        )
        if subagent.execution_trace_snippet:
            parts.append(f"Last snippet: {subagent.execution_trace_snippet}.")
    parts.append(f"Resume from `{interruption.resume_stage or task.pipeline_status}`.")
    return " ".join(parts)


def stale_interruption_reason(task: TaskRecord, stage: str, *, stale_pid: bool = False) -> str:
    active = task.runtime.execution.active_subagent
    if active is not None:
        pid_detail = f", pid {active.pid} no longer alive" if stale_pid and active.pid else ""
        return (
            f"Stale runner detected while subagent `{active.id}` "
            f"({active.role}/{active.engine}{pid_detail}) was still marked running in `{stage}`."
        )
    return f"Stale runner detected while `{stage}` was still marked running."


def recover_stale_runner_state(
    root: Path,
    *,
    summary: WorkspaceRepairSummary | None = None,
) -> bool:
    root = root.resolve()
    with workspace_lock(root):
        state = load_workspace_state(root)
        running_task_ids = _running_task_ids(root)
        if _can_skip_recovery_scan(
            root,
            state.active_task_id,
            running_task_ids,
            current_thread_owns_runner_guard=current_thread_owns_runner_guard(root),
            runner_lock_held=runner_lock_is_held(root),
            has_repair_candidates=_has_nonrunning_resumable_repair_candidates(root),
        ):
            return False
        # Repair must tolerate disk-only task dirs that are missing runtime
        # rows so one stale record does not block runner recovery.
        tasks = list_tasks(root, strict=False)
        tasks_by_id = {task.id: task for task in tasks}
        if not _can_attempt_stale_runner_recovery(root, tasks_by_id, running_task_ids):
            return False

        recovery = _recover_running_tasks(
            root,
            state,
            tasks_by_id,
            running_task_ids,
            summary=summary,
        )
        mutated = recovery["mutated"]
        transitioned = recovery["transitioned"]
        prioritized_ids = recovery["prioritized_ids"]
        journal_messages = recovery["journal_messages"]

        normalized = _normalize_nonrunning_resumable_tasks(
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

        if _update_active_task_after_recovery(
            root,
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


def _running_task_ids(root: Path) -> list[str]:
    with connect_workspace_db(root) as connection:
        try:
            rows = connection.execute(
                """
                SELECT task_id
                FROM task_state
                WHERE json_extract(payload, '$.runtime.pipeline.execution_status') = 'running'
                ORDER BY task_id
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [str(row["task_id"]) for row in rows]


def _should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    return task.pipeline_status == "commit_to_git" and task.status in {"queued", "in_progress", "interrupted"}


def _has_inactive_running_tasks(
    root: Path,
    tasks_by_id: dict[str, TaskRecord],
    timeout_seconds: float,
) -> bool:
    for task in tasks_by_id.values():
        if task.runtime.pipeline.execution_status != "running":
            continue
        ts_str = last_event_timestamp(root, task)
        if ts_str is None:
            continue
        try:
            event_time = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        if (datetime.now(UTC) - event_time).total_seconds() > timeout_seconds:
            return True
    return False


def _interrupted_subagent_snippet(root: Path, task: TaskRecord, active: RuntimeSubagentState) -> str:
    report = load_subagent_report(root, task.id, active.id)
    if report:
        summary = str(report.get("summary") or "").strip()
        if summary:
            return summary
    ref = next((candidate for candidate in reversed(task.subagents) if candidate.id == active.id), None)
    if ref is not None:
        trace = load_subagent_execution_trace(root, task, ref, active=True, runtime_state=active)
        snippet = "" if trace is None else summarize_transcript(trace.text)
        if snippet:
            return snippet
    return active.execution_trace_snippet or "runner interrupted before subagent completion"


def _interrupted_subagent_reason(task: TaskRecord, reason: str) -> str:
    active = task.runtime.execution.active_subagent
    interruption = task.runtime.execution.interruption
    last_interrupted = None if interruption is None else interruption.subagent
    if (
        last_interrupted is not None
        and last_interrupted.interruption_reason
        and (active is None or last_interrupted.id == active.id)
    ):
        return last_interrupted.interruption_reason
    return reason


def _write_interrupted_subagent_artifacts(
    root: Path,
    task: TaskRecord,
    subagent: RuntimeSubagentState,
    *,
    resume_stage: str,
) -> None:
    now = utcnow()
    session_payload = load_subagent_session(root, task.id, subagent.id)
    report_payload = load_subagent_report(root, task.id, subagent.id)
    session_payload.update(
        {
            "id": subagent.id,
            "role": subagent.role,
            "engine": subagent.engine,
            "status": subagent.status,
            "sandboxed": subagent.sandboxed,
            "sandbox": subagent.sandbox_summary or "host",
            "updated_at": now,
            "pid": subagent.pid,
            "exit_code": subagent.exit_code,
            "interruption_reason": subagent.interruption_reason or None,
            "resume_stage": resume_stage,
            "continuation": None if subagent.continuation is None else subagent.continuation.model_dump(mode="python"),
        }
    )
    report_payload["status"] = subagent.status
    report_payload["summary"] = report_payload.get("summary") or subagent.execution_trace_snippet
    report_payload["interruption_reason"] = subagent.interruption_reason or None
    report_payload["resume_stage"] = resume_stage
    report_payload["continuation"] = None
    if subagent.continuation is not None:
        report_payload["continuation"] = subagent.continuation.model_dump(mode="python")
    save_subagent_artifacts(
        root,
        task.id,
        subagent.id,
        session=session_payload,
        report=report_payload,
    )


def _interruption_timestamps(task: TaskRecord, now: str) -> dict[str, str | None]:
    started_at = task.runtime.pipeline.current_stage.started_at or task.runtime.pipeline.run_started_at
    interrupted_at = (
        task.runtime.execution.active_subagent.updated_at
        if task.runtime.execution.active_subagent is not None
        else task.runtime.pipeline.current_stage.updated_at or started_at or now
    )
    return {
        "run_started_at": task.runtime.pipeline.run_started_at,
        "stage_started_at": task.runtime.pipeline.current_stage.started_at,
        "started_at": started_at,
        "interrupted_at": interrupted_at,
    }


def _set_interruption_metadata(
    task: TaskRecord,
    *,
    root: Path,
    stage: str,
    summary: str,
    reason: str,
    now: str,
    started_at: str | None,
    run_started_at: str | None,
    stage_started_at: str | None,
    interrupted_at: str | None,
) -> None:
    interrupted_subagent = mark_interrupted_subagent(root, task, reason=reason, stage=stage)
    apply_task_outcome(
        task,
        kind="interrupted",
        stage=stage,
        reason_code="execution_interrupted",
        reason=summary,
        retry_count=task.runtime.pipeline.retry_count,
        retry_limit=task.runtime.pipeline.retry_limit,
    )
    task.runtime.execution.interruption = RuntimeInterruptionState(
        source="subagent" if interrupted_subagent is not None else "runner",
        stage=stage,
        pipeline_status=stage,
        resume_stage=stage,
        reason=reason,
        summary=summary,
        interrupted_at=interrupted_at,
        detected_at=now,
        run_started_at=run_started_at,
        stage_started_at=stage_started_at,
        subagent=interrupted_subagent,
    )
    task.runtime.pipeline.current_stage = task.runtime.pipeline.current_stage.model_copy(
        update={
            "stage": stage,
            "status": "interrupted",
            "started_at": started_at,
            "updated_at": now,
            "duration_seconds": duration_seconds(started_at, now),
        }
    )


def _can_attempt_stale_runner_recovery(
    root: Path,
    tasks_by_id: dict[str, TaskRecord],
    running_task_ids: list[str],
) -> bool:
    if len(running_task_ids) > 1:
        return False
    if not current_thread_owns_runner_guard(root) and runner_lock_is_held(root):
        if not runner_lock_pid_is_stale(root):
            config = load_config(root)
            if config.inactivity_timeout_seconds is None:
                return False
            if not _has_inactive_running_tasks(root, tasks_by_id, config.inactivity_timeout_seconds):
                return False
    return True


def _record_stale_recovery(
    root: Path,
    task: TaskRecord,
    *,
    stage: str,
    journal_message: str,
    summary: WorkspaceRepairSummary | None,
    stale_pid: bool,
) -> None:
    record_recovery_report(
        root,
        task,
        trigger_event_kind=TriggerEventKind.STALE_RUNNER_RECOVERY,
        origin_stage=stage,
        summary=journal_message,
        runnable_state="runnable",
        failure_classification="stale_runner",
        actions=[
            RecoveryAction(
                action="clear_stale_active_state", summary="Cleared stale active runner state for the task."
            ),
            RecoveryAction(action="requeue_stage", summary=f"Requeued the task at {stage}.", metadata={"stage": stage}),
        ],
        warnings=["stale subagent pid detected"] if stale_pid else [],
    )
    if summary is not None and task.id not in summary.requeued_task_ids:
        summary.requeued_task_ids.append(task.id)
    if stale_pid and summary is not None and task.id not in summary.stale_process_task_ids:
        summary.stale_process_task_ids.append(task.id)


def _recover_stale_running_task(
    root: Path,
    task: TaskRecord,
    *,
    summary: WorkspaceRepairSummary | None,
) -> tuple[bool, str | None, bool]:
    # inline: tasks.queue top-level-imports execution_recovery (would cycle).
    from litehive.tasks.queue import (  # noqa: PLC0415
        canonicalize_resumable_queue_task,
        is_task_eligible_for_execution,
        resumable_running_stage,
    )

    if not is_task_eligible_for_execution(task):
        return False, None, False
    stale_pid = subagent_process_is_stale(task)
    stage = resumable_running_stage(task)
    if stage is None:
        return False, None, False
    prepare_interrupted_task(
        root,
        task,
        stage=stage,
        summary=f"Interrupted run recovered after stale runner detection. Resume from `{stage}`.",
        reason=stale_interruption_reason(task, stage, stale_pid=stale_pid),
    )
    canonicalize_resumable_queue_task(task, stage=stage)
    _record_stale_recovery(
        root,
        task,
        stage=stage,
        journal_message=f"Recovered stale runner state and returned the task to `{stage}`.",
        summary=summary,
        stale_pid=stale_pid,
    )
    return True, interruption_journal_message(task), True


def _can_skip_recovery_scan(
    root: Path,
    active_task_id: str | None,
    running_task_ids: list[str],
    *,
    current_thread_owns_runner_guard: bool,
    runner_lock_held: bool,
    has_repair_candidates: bool,
) -> bool:
    del root
    return (
        not running_task_ids
        and active_task_id is None
        and not current_thread_owns_runner_guard
        and not runner_lock_held
        and not has_repair_candidates
    )


def _recover_running_tasks(
    root: Path,
    state,
    tasks_by_id: dict[str, TaskRecord],
    running_task_ids: list[str],
    *,
    summary: WorkspaceRepairSummary | None,
) -> dict[str, object]:
    mutated = False
    transitioned: list[TaskRecord] = []
    journal_messages: dict[str, str] = {}
    prioritized_ids: list[str] = []
    for task_id in running_task_ids:
        task = tasks_by_id.get(task_id)
        if task is None:
            continue
        if task_id != state.active_task_id and not _should_requeue_commit_stage_task(task):
            if state.active_task_id is not None:
                metadata = read_runner_lock_metadata(root)
                if not runner_metadata_present(metadata):
                    continue
        task_mutated, journal_message, prioritize = _recover_stale_running_task(root, task, summary=summary)
        if not task_mutated:
            continue
        transitioned.append(task)
        mutated = True
        if journal_message is not None:
            journal_messages[task.id] = journal_message
        if prioritize:
            prioritized_ids.append(task.id)
    return {
        "mutated": mutated,
        "transitioned": transitioned,
        "journal_messages": journal_messages,
        "prioritized_ids": prioritized_ids,
    }


def _normalize_nonrunning_resumable_tasks(
    state,
    *,
    tasks_by_id: dict[str, TaskRecord],
    summary: WorkspaceRepairSummary | None,
) -> dict[str, object]:
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
        if task.runtime.pipeline.execution_status == "running":
            continue
        if task.status not in {"queued", "in_progress", "interrupted"}:
            continue
        has_resume_marker = task_has_resume_marker(task)
        # Crash cleanup can leave a task queued with execution_status
        # "interrupted" and a trusted stage marker. That state is not eligible
        # for normal dequeue, but repair must still canonicalize it back to a
        # runnable queued/idle task.
        if task.status != "interrupted" and not is_task_eligible_for_execution(task) and not has_resume_marker:
            continue
        if task.status == "queued" and task.id != state.active_task_id and not has_resume_marker:
            continue
        stage = resumable_queue_stage(task)
        if stage is None:
            continue
        queue_contains_task = task.id in state.queue
        queue_index = None if not queue_contains_task else state.queue.index(task.id)
        should_normalize = (
            task.status != "queued"
            or task.runtime.pipeline.execution_status != "idle"
            or task.pipeline_status != stage
            or task.runtime.pipeline.current_stage.stage != stage
            or task.runtime.pipeline.current_stage.status != "idle"
            or task.id == state.active_task_id
            or not queue_contains_task
        )
        if not should_normalize:
            continue

        was_in_progress = task.status == "in_progress"
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


def _has_nonrunning_resumable_repair_candidates(root: Path) -> bool:
    with connect_workspace_db(root) as connection:
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


def _update_active_task_after_recovery(
    root: Path,
    state,
    *,
    tasks_by_id: dict[str, TaskRecord],
    prioritized_ids: list[str],
    running_task_ids: list[str],
    summary: WorkspaceRepairSummary | None,
) -> bool:
    mutated = False
    if prioritized_ids:
        state.queue = [task_id for task_id in state.queue if task_id not in running_task_ids]
        state.queue = [*prioritized_ids, *state.queue]
        mutated = True
    if state.active_task_id is None:
        return mutated
    active_task = tasks_by_id.get(state.active_task_id)
    active_task_missing = state.active_task_id not in tasks_by_id and not _task_state_row_exists(
        root, state.active_task_id
    )
    should_clear_active_task_id = (
        active_task_missing
        or state.active_task_id in prioritized_ids
        or (
            active_task is not None
            and active_task.runtime.pipeline.execution_status != "running"
            and active_task.id not in running_task_ids
            and not _should_requeue_commit_stage_task(active_task)
        )
    )
    if should_clear_active_task_id:
        if (
            summary is not None
            and summary.cleared_active_task_id is None
            and not (active_task is not None and _should_requeue_commit_stage_task(active_task))
        ):
            summary.cleared_active_task_id = state.active_task_id
        state.active_task_id = None
        mutated = True
    return mutated


def _task_state_row_exists(root: Path, task_id: str) -> bool:
    with connect_workspace_db(root) as connection:
        try:
            row = connection.execute(
                "SELECT 1 FROM task_state WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
    return row is not None
