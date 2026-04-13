"""Workspace-level recovery and stale-runner repair."""

import re
import sqlite3
from pathlib import Path

import yaml

from litehive.config.loading import load_config
from litehive.models.common import utcnow
from litehive.models.report_models import RecoveryAction
from litehive.models.runtime_models import (
    RuntimeContinuationHandoff,
    RuntimeInterruptionState,
    RuntimeSubagentState,
)
from litehive.models.task_models import TaskRecord
from litehive.tasks.models import WorkspaceRepairSummary
from litehive.tasks.paths import (
    read_text_artifact,
    resolve_artifact_path,
    task_dir,
)
from litehive.tasks.runtime import (
    apply_task_outcome,
    duration_seconds,
    summarize_transcript,
)

from .detection import has_inactive_running_tasks, is_stranded_commit_task, should_requeue_commit_stage_task


def _running_task_ids(root: Path) -> list[str]:
    from litehive.db.schema import connect_workspace_db

    with connect_workspace_db(root) as connection:
        try:
            rows = connection.execute(
                """
                SELECT task_id
                FROM task_state
                WHERE json_extract(payload, '$.runtime.execution_status') = 'running'
                ORDER BY task_id
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [str(row["task_id"]) for row in rows]


def _load_comment_entries(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        return []
    return [dict(entry) for entry in loaded if isinstance(entry, dict)]


def _migrate_legacy_thread_files(
    root: Path,
    *,
    summary: WorkspaceRepairSummary | None = None,
) -> bool:
    from litehive.tasks.persistence import atomic_write_text
    from litehive.tasks.paths import tasks_root

    mutated = False
    for task_path in sorted(tasks_root(root).iterdir()):
        if not task_path.is_dir():
            continue
        legacy_path = task_path / "thread.yaml"
        if not legacy_path.exists():
            continue
        comments_path = task_path / "comments.yaml"

        legacy_entries = _load_comment_entries(legacy_path)
        comment_entries = _load_comment_entries(comments_path)
        merged_entries = list(legacy_entries)
        for entry in comment_entries:
            if entry not in merged_entries:
                merged_entries.append(entry)

        if merged_entries != comment_entries:
            atomic_write_text(
                comments_path,
                yaml.safe_dump(merged_entries, sort_keys=False),
            )
        legacy_path.unlink()
        mutated = True
        if summary is not None:
            match = re.match(r"^(T-\d{4})-", task_path.name)
            task_id = match.group(1) if match else task_path.name
            if task_id not in summary.migrated_comment_task_ids:
                summary.migrated_comment_task_ids.append(task_id)
    return mutated


def prepare_interrupted_task_for_requeue(task: TaskRecord) -> None:
    now = utcnow()
    task.status = "queued"
    task.runtime.execution_status = "idle"
    task.runtime.run_started_at = None
    task.runtime.active_subagent = None
    task.runtime.updated_at = now
    if task.runtime.current_stage.step is None:
        task.runtime.current_stage = task.runtime.current_stage.model_copy(
            update={
                "status": "idle",
                "started_at": None,
                "completed_at": None,
                "updated_at": now,
                "duration_seconds": 0,
                "verdict": None,
                "summary": "",
            }
        )
        return
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={"status": "interrupted", "updated_at": now}
    )


def _interrupted_subagent_snippet(
    root: Path, task: TaskRecord, active: RuntimeSubagentState
) -> str:
    subagent_base = task_dir(root, task) / active.path
    report_path = subagent_base / "report.yaml"
    if report_path.exists():
        try:
            report = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            report = {}
        if isinstance(report, dict):
            summary = str(report.get("summary") or "").strip()
            if summary:
                return summary
    transcript_path = resolve_artifact_path(subagent_base, "transcript.md")
    if transcript_path is not None:
        snippet = summarize_transcript(read_text_artifact(transcript_path))
        if snippet:
            return snippet
    return active.transcript_snippet or "runner interrupted before subagent completion"


def _interrupted_subagent_reason(task: TaskRecord, reason: str) -> str:
    active = task.runtime.active_subagent
    last_subagent = task.runtime.last_subagent
    if (
        last_subagent is not None
        and last_subagent.interruption_reason
        and (active is None or last_subagent.id == active.id)
    ):
        return last_subagent.interruption_reason
    return reason


def _write_interrupted_subagent_artifacts(
    root: Path,
    task: TaskRecord,
    subagent: RuntimeSubagentState,
    *,
    resume_stage: str,
) -> None:
    from litehive.tasks.persistence import write_atomic_files

    now = utcnow()
    base = task_dir(root, task) / subagent.path
    session_path = base / "session.yaml"
    report_path = base / "report.yaml"
    writes: dict[Path, str] = {}
    session_payload = (
        yaml.safe_load(session_path.read_text(encoding="utf-8")) if session_path.exists() else {}
    ) or {}
    report_payload = (
        yaml.safe_load(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    ) or {}
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
            "continuation": None
            if subagent.continuation is None
            else subagent.continuation.model_dump(mode="python"),
        }
    )
    report_payload["status"] = subagent.status
    report_payload["summary"] = report_payload.get("summary") or subagent.transcript_snippet
    report_payload["interruption_reason"] = subagent.interruption_reason or None
    report_payload["resume_stage"] = resume_stage
    report_payload["continuation"] = None
    if subagent.continuation is not None:
        report_payload["continuation"] = subagent.continuation.model_dump(mode="python")
    writes[session_path] = yaml.safe_dump(session_payload, sort_keys=False)
    writes[report_path] = yaml.safe_dump(report_payload, sort_keys=False)
    write_atomic_files(writes)


def mark_interrupted_subagent(
    root: Path, task: TaskRecord, *, reason: str, stage: str
) -> RuntimeSubagentState | None:
    active = task.runtime.active_subagent
    existing = task.runtime.last_subagent if task.runtime.last_subagent is not None else None
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
    snippet = source.transcript_snippet
    if active is not None or not snippet:
        snippet = _interrupted_subagent_snippet(root, task, source)
    interrupted = source.model_copy(
        update={
            "status": "interrupted",
            "updated_at": now,
            "completed_at": source.completed_at or now,
            "transcript_snippet": snippet,
            "interruption_reason": _interrupted_subagent_reason(task, reason),
        }
    )
    task.runtime.last_subagent = interrupted
    task.runtime.active_subagent = None
    _write_interrupted_subagent_artifacts(root, task, interrupted, resume_stage=stage)
    return interrupted


def _interruption_timestamps(task: TaskRecord, now: str) -> dict[str, str | None]:
    started_at = task.runtime.current_stage.started_at or task.runtime.run_started_at
    interrupted_at = (
        task.runtime.active_subagent.updated_at
        if task.runtime.active_subagent is not None
        else task.runtime.current_stage.updated_at or started_at or now
    )
    return {
        "run_started_at": task.runtime.run_started_at,
        "stage_started_at": task.runtime.current_stage.started_at,
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
        retry_count=task.runtime.retry_count,
        retry_limit=task.runtime.retry_limit,
    )
    task.runtime.interruption = RuntimeInterruptionState(
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
    task.runtime.continuation_handoff = RuntimeContinuationHandoff(
        step=stage,
        kind="restart",
        reason=reason,
        from_engine=None if interrupted_subagent is None else interrupted_subagent.engine,
        to_engine=None if interrupted_subagent is None else interrupted_subagent.engine,
        subagent_id=None if interrupted_subagent is None else interrupted_subagent.id,
        subagent_path=None if interrupted_subagent is None else interrupted_subagent.path,
        status=None if interrupted_subagent is None else interrupted_subagent.status,
        summary=summary,
        transcript_snippet=""
        if interrupted_subagent is None
        else interrupted_subagent.transcript_snippet,
        warnings=[],
        session_path=None if interrupted_subagent is None else f"{interrupted_subagent.path}/session.yaml",
        report_path=None if interrupted_subagent is None else f"{interrupted_subagent.path}/report.yaml",
        transcript_path=None
        if interrupted_subagent is None
        else f"{interrupted_subagent.path}/transcript.md",
        continuation=None if interrupted_subagent is None else interrupted_subagent.continuation,
        updated_at=now,
    )
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": stage,
            "status": "interrupted",
            "started_at": started_at,
            "completed_at": now,
            "updated_at": now,
            "duration_seconds": duration_seconds(started_at, now),
            "verdict": "blocked",
            "summary": summary,
        }
    )


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
    task.runtime.execution_status = "interrupted"
    task.runtime.run_started_at = None
    task.runtime.updated_at = now
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
    interruption = task.runtime.interruption
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
        if subagent.transcript_snippet:
            parts.append(f"Last snippet: {subagent.transcript_snippet}.")
    parts.append(f"Resume from `{interruption.resume_stage or task.pipeline_status}`.")
    return " ".join(parts)


def stale_interruption_reason(task: TaskRecord, stage: str, *, stale_pid: bool = False) -> str:
    active = task.runtime.active_subagent
    if active is not None:
        pid_detail = f", pid {active.pid} no longer alive" if stale_pid and active.pid else ""
        return (
            f"Stale runner detected while subagent `{active.id}` "
            f"({active.role}/{active.engine}{pid_detail}) was still marked running in `{stage}`."
        )
    return f"Stale runner detected while `{stage}` was still marked running."


def _can_attempt_stale_runner_recovery(
    root: Path, tasks_by_id: dict[str, TaskRecord], running_task_ids: list[str]
) -> bool:
    from litehive.state.locking import (
        current_thread_owns_runner_guard,
        runner_lock_is_held,
        runner_lock_pid_is_stale,
    )

    if len(running_task_ids) > 1:
        return False
    if not current_thread_owns_runner_guard(root) and runner_lock_is_held(root):
        if not runner_lock_pid_is_stale(root):
            config = load_config(root)
            if config.inactivity_timeout_seconds is None:
                return False
            if not has_inactive_running_tasks(root, tasks_by_id, config.inactivity_timeout_seconds):
                return False
    return True


def _record_commit_stale_recovery(
    root: Path,
    task: TaskRecord,
    *,
    journal_message: str,
    finalized: bool,
    summary: WorkspaceRepairSummary | None,
    stale_pid: bool,
) -> None:
    from litehive.tasks.reports import record_recovery_report

    record_recovery_report(
        root,
        task,
        trigger="stale_runner_recovery",
        stage="commit_to_git",
        summary=journal_message,
        runnable_state="runnable",
        failure_classification="stale_runner",
        actions=_commit_stale_recovery_actions(task, finalized=finalized),
        warnings=["stale subagent pid detected"] if stale_pid else [],
    )
    if finalized:
        if summary is not None and task.id not in summary.finalized_commit_task_ids:
            summary.finalized_commit_task_ids.append(task.id)
    elif summary is not None and task.id not in summary.requeued_task_ids:
        summary.requeued_task_ids.append(task.id)
    if stale_pid and summary is not None and task.id not in summary.stale_process_task_ids:
        summary.stale_process_task_ids.append(task.id)


def _commit_stale_recovery_actions(task: TaskRecord, *, finalized: bool) -> list[RecoveryAction]:
    actions = [RecoveryAction(action="clear_stale_active_state", summary="Cleared stale active runner state for the task.")]
    if finalized:
        actions.append(
            RecoveryAction(
                action="finalize_existing_checkpoint",
                summary="Recorded the existing checkpoint commit and finalized the task.",
                metadata={"commit_sha": task.git.commit_sha},
            )
        )
        return actions
    actions.append(
        RecoveryAction(
            action="requeue_stage",
            summary="Requeued the task at commit_to_git.",
            metadata={"stage": "commit_to_git"},
        )
    )
    return actions


def _recover_stale_running_task(
    root: Path,
    task: TaskRecord,
    *,
    summary: WorkspaceRepairSummary | None,
) -> tuple[bool, str | None, bool]:
    from litehive.tasks.queue import is_task_eligible_for_execution
    from litehive.state.locking import subagent_process_is_stale

    if not is_task_eligible_for_execution(task):
        return False, None, False
    stale_pid = subagent_process_is_stale(task)
    if is_stranded_commit_task(task):
        return False, None, stale_pid
    stage = task.pipeline_status
    if should_requeue_commit_stage_task(task):
        prepare_interrupted_task(
            root,
            task,
            stage="commit_to_git",
            summary="Interrupted `commit_to_git` run recovered. Resume from `commit_to_git`.",
            reason=stale_interruption_reason(task, "commit_to_git", stale_pid=stale_pid),
        )
        task.status = "queued"
        journal_message = interruption_journal_message(task)
        _record_commit_stale_recovery(
            root,
            task,
            journal_message=journal_message,
            finalized=task.pipeline_status == "done",
            summary=summary,
            stale_pid=stale_pid,
        )
        return True, journal_message, task.status == "queued"
    prepare_interrupted_task(
        root,
        task,
        stage=stage,
        summary=f"Interrupted run recovered after stale runner detection. Resume from `{stage}`.",
        reason=stale_interruption_reason(task, stage, stale_pid=stale_pid),
    )
    from litehive.tasks.reports import record_recovery_report

    record_recovery_report(
        root,
        task,
        trigger="stale_runner_recovery",
        stage=task.pipeline_status,
        summary=f"Recovered stale runner state and returned the task to `{task.pipeline_status}`.",
        runnable_state="runnable",
        failure_classification="stale_runner",
        actions=[
            RecoveryAction(action="clear_stale_active_state", summary="Cleared stale active runner state for the task."),
            RecoveryAction(
                action="requeue_stage",
                summary=f"Requeued the task at {task.pipeline_status}.",
                metadata={"stage": task.pipeline_status},
            ),
        ],
        warnings=["stale subagent pid detected"] if stale_pid else [],
    )
    if summary is not None and task.id not in summary.requeued_task_ids:
        summary.requeued_task_ids.append(task.id)
    if stale_pid and summary is not None and task.id not in summary.stale_process_task_ids:
        summary.stale_process_task_ids.append(task.id)
    return True, interruption_journal_message(task), True


def recover_stale_runner_state(
    root: Path,
    *,
    summary: WorkspaceRepairSummary | None = None,
) -> bool:
    from litehive.state.records import list_tasks
    from litehive.tasks.persistence import save_state_without_runner_guard, load_state
    from litehive.state.locking import (
        current_thread_owns_runner_guard,
        runner_lock_is_held,
        workspace_lock,
    )
    from litehive.state.persist import persist_tasks_and_state_without_runner_guard

    root = root.resolve()
    with workspace_lock(root):
        state = load_state(root)
        running_task_ids = _running_task_ids(root)
        if not running_task_ids and state.active_task_id is None:
            if not current_thread_owns_runner_guard(root) and not runner_lock_is_held(root):
                return False
        tasks = list_tasks(root)
        tasks_by_id = {task.id: task for task in tasks}
        if not _can_attempt_stale_runner_recovery(root, tasks_by_id, running_task_ids):
            return False
        mutated = False
        transitioned: list[TaskRecord] = []
        journal_messages: dict[str, str] = {}
        prioritized_ids: list[str] = []
        for task_id in running_task_ids:
            task = tasks_by_id.get(task_id)
            if task is None:
                continue
            if task_id != state.active_task_id and not should_requeue_commit_stage_task(task):
                if state.active_task_id is not None:
                    from litehive.state.locking import read_runner_lock_metadata, runner_metadata_present

                    if not runner_metadata_present(read_runner_lock_metadata(root)):
                        continue
            task_mutated, journal_message, prioritize = _recover_stale_running_task(
                root, task, summary=summary
            )
            if not task_mutated:
                continue
            transitioned.append(task)
            mutated = True
            if journal_message is not None:
                journal_messages[task.id] = journal_message
            if prioritize:
                prioritized_ids.append(task.id)
        if transitioned:
            if state.active_task_id is not None:
                active_task = tasks_by_id.get(state.active_task_id)
                should_report_clear = not (
                    active_task is not None
                    and (is_stranded_commit_task(active_task) or should_requeue_commit_stage_task(active_task))
                )
                if summary is not None and summary.cleared_active_task_id is None and should_report_clear:
                    summary.cleared_active_task_id = state.active_task_id
                state.active_task_id = None
            mutated = True
            state.queue = [task_id for task_id in state.queue if task_id not in running_task_ids]
            if prioritized_ids:
                state.queue = [*prioritized_ids, *state.queue]
        if state.active_task_id is not None and (
            state.active_task_id not in tasks_by_id or state.active_task_id in prioritized_ids
        ):
            active_task = tasks_by_id.get(state.active_task_id)
            should_report_clear = not (
                active_task is not None
                and (is_stranded_commit_task(active_task) or should_requeue_commit_stage_task(active_task))
            )
            if summary is not None and summary.cleared_active_task_id is None and should_report_clear:
                summary.cleared_active_task_id = state.active_task_id
            state.active_task_id = None
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


def repair_workspace_state(root: Path) -> WorkspaceRepairSummary:
    summary = WorkspaceRepairSummary()
    summary.stale_runner_recovered = recover_stale_runner_state(root, summary=summary)
    summary.mutated = summary.stale_runner_recovered
    if _migrate_legacy_thread_files(root, summary=summary):
        summary.mutated = True
    return summary
