"""Recovery: stale runner detection, interrupted task handling, workspace repair."""

from pathlib import Path

import yaml

from litehive.config import load_config
from litehive.git import (
    GitError,
    checkpoint_message,
    find_commit_by_subject,
    is_git_repo,
)
from litehive.models import (
    RecoveryAction,
    RuntimeContinuationHandoff,
    RuntimeInterruptionState,
    RuntimeSubagentState,
    TaskRecord,
    WorkspaceState,
    utcnow,
)

from litehive.tasks.models import WorkspaceRepairSummary
from litehive.tasks.paths import (
    _read_text_artifact,
    _resolve_artifact_path,
    task_dir,
)
from litehive.workspace.runtime_tracking import _apply_task_outcome, _duration_seconds, summarize_transcript


def _is_stranded_commit_task(task: TaskRecord) -> bool:
    return (
        task.pipeline_status == "done"
        and task.git.commit_sha is None
        and task.git.checkpoint_attempts > 0
    )


def _is_orphaned_commit_stage_task(task: TaskRecord, state: WorkspaceState) -> bool:
    return (
        task.pipeline_status == "commit_to_git"
        and task.status in {"queued", "in_progress", "interrupted"}
        and state.active_task_id != task.id
        and task.id not in state.queue
    )


def _should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    return task.pipeline_status == "commit_to_git" and task.status in {
        "queued",
        "in_progress",
        "interrupted",
    }


def _prepare_recovered_commit_task(task: TaskRecord) -> None:
    now = utcnow()
    task.status = "queued"
    task.pipeline_status = "commit_to_git"
    task.runtime.execution_status = "idle"
    task.runtime.run_started_at = None
    task.runtime.active_subagent = None
    task.runtime.updated_at = now
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )


def _prepare_interrupted_task_for_requeue(task: TaskRecord) -> None:
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
    else:
        task.runtime.current_stage = task.runtime.current_stage.model_copy(
            update={
                "status": "interrupted",
                "updated_at": now,
            }
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
    transcript_path = subagent_base / "transcript.md"
    transcript_path = _resolve_artifact_path(subagent_base, "transcript.md")
    if transcript_path is not None:
        transcript = _read_text_artifact(transcript_path)
        snippet = summarize_transcript(transcript)
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
    from litehive.tasks.persistence import _write_atomic_files

    now = utcnow()
    base = task_dir(root, task) / subagent.path
    session_path = base / "session.yaml"
    report_path = base / "report.yaml"
    writes: dict[Path, str] = {}

    session_payload: dict[str, object]
    if session_path.exists():
        existing_session = yaml.safe_load(session_path.read_text(encoding="utf-8")) or {}
        session_payload = existing_session if isinstance(existing_session, dict) else {}
    else:
        session_payload = {}
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
            "continuation": (
                None
                if subagent.continuation is None
                else subagent.continuation.model_dump(mode="python")
            ),
        }
    )
    writes[session_path] = yaml.safe_dump(session_payload, sort_keys=False)

    report_payload: dict[str, object]
    if report_path.exists():
        existing_report = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        report_payload = existing_report if isinstance(existing_report, dict) else {}
    else:
        report_payload = {}
    report_payload["status"] = subagent.status
    report_payload["summary"] = report_payload.get("summary") or subagent.transcript_snippet
    report_payload["interruption_reason"] = subagent.interruption_reason or None
    report_payload["resume_stage"] = resume_stage
    report_payload["continuation"] = (
        None if subagent.continuation is None else subagent.continuation.model_dump(mode="python")
    )
    writes[report_path] = yaml.safe_dump(report_payload, sort_keys=False)
    _write_atomic_files(writes)


def _mark_interrupted_subagent(
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
    snippet = (
        _interrupted_subagent_snippet(root, task, source)
        if active is not None or not source.transcript_snippet
        else source.transcript_snippet
    )
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


def _prepare_interrupted_task(
    root: Path,
    task: TaskRecord,
    *,
    stage: str,
    summary: str,
    reason: str | None = None,
) -> None:
    now = utcnow()
    run_started_at = task.runtime.run_started_at
    stage_started_at = task.runtime.current_stage.started_at
    started_at = task.runtime.current_stage.started_at or task.runtime.run_started_at
    interrupted_at = (
        task.runtime.active_subagent.updated_at
        if task.runtime.active_subagent is not None
        else task.runtime.current_stage.updated_at or started_at or now
    )
    task.status = "interrupted"
    task.pipeline_status = stage  # type: ignore[assignment]
    task.runtime.execution_status = "interrupted"
    task.runtime.run_started_at = None
    interruption_reason = reason or summary
    interrupted_subagent = _mark_interrupted_subagent(
        root, task, reason=interruption_reason, stage=stage
    )
    _apply_task_outcome(
        task,
        kind="interrupted",
        stage=stage,
        reason_code="execution_interrupted",
        reason=summary,
        retry_count=task.runtime.retry_count,
        retry_limit=task.runtime.retry_limit,
        retry_source=task.runtime.retry_source,
    )
    task.runtime.updated_at = now
    task.runtime.interruption = RuntimeInterruptionState(
        source="subagent" if interrupted_subagent is not None else "runner",
        stage=stage,
        pipeline_status=stage,
        resume_stage=stage,
        reason=interruption_reason,
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
        reason=interruption_reason,
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
        session_path=(
            None if interrupted_subagent is None else f"{interrupted_subagent.path}/session.yaml"
        ),
        report_path=(
            None if interrupted_subagent is None else f"{interrupted_subagent.path}/report.yaml"
        ),
        transcript_path=(
            None if interrupted_subagent is None else f"{interrupted_subagent.path}/transcript.md"
        ),
        continuation=(None if interrupted_subagent is None else interrupted_subagent.continuation),
        updated_at=now,
    )
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": stage,
            "status": "interrupted",
            "started_at": started_at,
            "completed_at": now,
            "updated_at": now,
            "duration_seconds": _duration_seconds(started_at, now),
            "verdict": "blocked",
            "summary": summary,
        }
    )


def interruption_journal_message(task: TaskRecord) -> str:
    interruption = task.runtime.interruption
    if interruption is None:
        return f"Interrupted run recorded. Resume from `{task.pipeline_status}`."
    parts = [
        (
            f"Interrupted {interruption.source} execution while `{interruption.stage or task.pipeline_status}` "
            f"was running."
        ),
        f"Reason: {interruption.reason or interruption.summary or 'unknown'}.",
    ]
    if interruption.subagent is not None:
        subagent = interruption.subagent
        pid = subagent.pid if subagent.pid is not None else "-"
        parts.append(
            (
                f"Subagent `{subagent.id}` ({subagent.role}/{subagent.engine}, pid={pid}, "
                f"path `{subagent.path}`) stopped with status `{subagent.status}`."
            )
        )
        if subagent.transcript_snippet:
            parts.append(f"Last snippet: {subagent.transcript_snippet}.")
    parts.append(f"Resume from `{interruption.resume_stage or task.pipeline_status}`.")
    return " ".join(parts)


def _stale_interruption_reason(task: TaskRecord, stage: str, *, stale_pid: bool = False) -> str:
    active = task.runtime.active_subagent
    if active is not None:
        pid_detail = ""
        if stale_pid and active.pid is not None:
            pid_detail = f", pid {active.pid} no longer alive"
        return (
            f"Stale runner detected while subagent `{active.id}` "
            f"({active.role}/{active.engine}{pid_detail}) was still marked running in `{stage}`."
        )
    return f"Stale runner detected while `{stage}` was still marked running."


def _recover_commit_task(root: Path, task: TaskRecord) -> str:
    summary = "Interrupted `commit_to_git` run recovered. Resume from `commit_to_git`."
    _prepare_interrupted_task(
        root,
        task,
        stage="commit_to_git",
        summary=summary,
        reason=_stale_interruption_reason(task, "commit_to_git"),
    )
    task.status = "queued"
    return interruption_journal_message(task)


def _latest_stage_report_verdict(root: Path, task: TaskRecord) -> str | None:
    reports_dir = task_dir(root, task) / "reports"
    if not reports_dir.exists():
        return None
    reports = sorted(reports_dir.glob("*.yaml"))
    if not reports:
        return None
    return _report_verdict(reports[-1])


def _latest_stage_report_verdict_for_step(root: Path, task: TaskRecord, step: str) -> str | None:
    reports_dir = task_dir(root, task) / "reports"
    if not reports_dir.exists():
        return None
    reports = sorted(reports_dir.glob(f"{step}-*.yaml"))
    if not reports:
        return None
    return _report_verdict(reports[-1])


def _report_verdict(path: Path) -> str | None:
    try:
        report = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    verdict = str(report.get("verdict") or "").strip().lower()
    return verdict or None


def _should_recover_flagged_commit_stage_task(root: Path, task: TaskRecord) -> bool:
    if task.pipeline_status != "commit_to_git" or task.status != "flagged":
        return False
    if task.git.commit_sha is not None:
        return False
    if task.git.merge_agent_attempts >= 1:
        return False
    if _latest_stage_report_verdict_for_step(root, task, "accepting") in {"pass", "accept"}:
        return True
    if _latest_stage_report_verdict(root, task) in {"pass", "accept"}:
        return True
    return _latest_stage_report_verdict_for_step(root, task, "testing") in {"pass", "accept"}


def _should_resume_done_task_at_commit_stage(root: Path, task: TaskRecord) -> bool:
    if task.status != "done" or task.pipeline_status != "done":
        return False
    if task.git.commit_sha is not None:
        return False
    config = load_config(root)
    if not config.auto_commit or not task.git.auto_commit:
        return False
    return _latest_stage_report_verdict_for_step(root, task, "accepting") in {"pass", "accept"}


def _recover_flagged_commit_task(task: TaskRecord) -> str:
    _prepare_recovered_commit_task(task)
    return "Recovered flagged accepted task back to `queued/commit_to_git` for final checkpoint commit."


def _finalize_recovered_commit_task(task: TaskRecord, *, commit_sha: str) -> str:
    from litehive.tasks.crud import set_task_commit_sha

    now = utcnow()
    started_at = task.runtime.current_stage.started_at
    task.status = "done"
    task.pipeline_status = "done"
    set_task_commit_sha(task, commit_sha)
    task.runtime.execution_status = "done"
    task.runtime.run_started_at = None
    task.runtime.active_subagent = None
    task.runtime.updated_at = now
    task.runtime.last_stage = task.runtime.last_stage.model_copy(
        update={
            "step": "commit_to_git",
            "status": "completed",
            "started_at": started_at,
            "completed_at": now,
            "updated_at": now,
            "duration_seconds": _duration_seconds(started_at, now),
            "verdict": "pass",
            "summary": "Recovered existing checkpoint commit after interrupted `commit_to_git`.",
        }
    )
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    return (
        "Recovered existing checkpoint commit after interrupted `commit_to_git` "
        f"and finalized the task at `{commit_sha}`."
    )


def _find_existing_checkpoint_commit(root: Path, task: TaskRecord) -> str | None:
    if not is_git_repo(root):
        return None
    try:
        return find_commit_by_subject(
            root,
            checkpoint_message(task, attempt=task.git.checkpoint_attempts),
        )
    except GitError:
        return None


def _recover_existing_checkpoint_commit(root: Path, task: TaskRecord) -> str | None:
    commit_sha = _find_existing_checkpoint_commit(root, task)
    if commit_sha is None:
        return None
    return _finalize_recovered_commit_task(task, commit_sha=commit_sha)


def _recover_stranded_commit_tasks(root: Path, state: WorkspaceState) -> bool:
    from litehive.tasks.crud import list_tasks
    from .workflow import _persist_tasks_and_state_without_runner_guard

    tasks = list_tasks(root)
    stranded = [task for task in tasks if _is_stranded_commit_task(task)]
    accepted_without_commit = {
        task.id: task for task in tasks if _should_resume_done_task_at_commit_stage(root, task)
    }
    orphaned = [task for task in tasks if _is_orphaned_commit_stage_task(task, state)]
    flagged_commit_ready = {
        task.id: task for task in tasks if _should_recover_flagged_commit_stage_task(root, task)
    }
    completed_ids: set[str] = set()
    recovered: list[TaskRecord] = []
    transitioned: list[TaskRecord] = []
    journal_messages: dict[str, str] = {}
    for task in stranded:
        journal_message = _recover_existing_checkpoint_commit(root, task)
        if journal_message is not None:
            completed_ids.add(task.id)
            transitioned.append(task)
            journal_messages[task.id] = journal_message
            continue
        recovered.append(task)
    recovered.extend(
        task for task_id, task in accepted_without_commit.items() if task_id not in completed_ids
    )
    recovered.extend(orphaned)
    recovered.extend(flagged_commit_ready.values())
    recovered_ids = {task.id for task in recovered}
    resolved_ids = {*recovered_ids, *completed_ids}
    queue = [task_id for task_id in state.queue if task_id not in recovered_ids]
    for task in recovered:
        if task.id in accepted_without_commit:
            journal_messages[task.id] = (
                "Recovered accepted task back to `queued/commit_to_git` "
                "because no final checkpoint commit was recorded."
            )
            _prepare_recovered_commit_task(task)
        elif task.id in flagged_commit_ready:
            journal_messages[task.id] = _recover_flagged_commit_task(task)
        else:
            journal_messages[task.id] = _recover_commit_task(root, task)
        transitioned.append(task)
    queue = [task_id for task_id in queue if task_id not in completed_ids]
    if recovered:
        queue = [*(task.id for task in recovered), *queue]
    if state.active_task_id in resolved_ids:
        state.active_task_id = None
    state.queue = queue
    if not transitioned:
        return False
    _persist_tasks_and_state_without_runner_guard(
        root,
        tasks=transitioned,
        state=state,
        journal_messages=journal_messages,
    )
    return True


def _has_inactive_running_tasks(
    root: Path,
    tasks_by_id: dict[str, TaskRecord],
    timeout_seconds: float,
) -> bool:
    """Check whether any running task has been inactive based on last event timestamp."""
    from datetime import UTC, datetime

    from litehive.observability.events import last_event_timestamp

    for task_id, task in tasks_by_id.items():
        if task.runtime.execution_status != "running":
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
        now = datetime.now(UTC)
        elapsed = (now - event_time).total_seconds()
        if elapsed > timeout_seconds:
            return True
    return False


def _recover_stale_runner_state(
    root: Path,
    *,
    summary: WorkspaceRepairSummary | None = None,
) -> bool:
    from litehive.tasks.crud import list_tasks
    from .locking import (
        _current_thread_owns_runner_guard,
        _read_runner_lock_metadata,
        _runner_lock_is_held,
        _runner_lock_pid_is_stale,
        _runner_metadata_present,
        _subagent_process_is_stale,
        _workspace_lock,
    )
    from litehive.tasks.persistence import _save_state_without_runner_guard, load_state
    from litehive.tasks.queue_ops import _is_task_eligible_for_execution
    from litehive.tasks.reports import record_recovery_report
    from .workflow import _persist_tasks_and_state_without_runner_guard

    root = root.resolve()
    with _workspace_lock(root):
        state = load_state(root)
        tasks = list_tasks(root)
        tasks_by_id = {task.id: task for task in tasks}
        running_task_ids = sorted(
            task.id for task in tasks if task.runtime.execution_status == "running"
        )
        if not _current_thread_owns_runner_guard(root) and _runner_lock_is_held(root):
            if not _runner_lock_pid_is_stale(root):
                config = load_config(root)
                if config.inactivity_timeout_seconds is None:
                    return False
                if not _has_inactive_running_tasks(
                    root, tasks_by_id, config.inactivity_timeout_seconds
                ):
                    return False

        runner_metadata = _read_runner_lock_metadata(root)
        has_stale_metadata = _runner_metadata_present(runner_metadata)

        mutated = False
        transitioned: list[TaskRecord] = []
        journal_messages: dict[str, str] = {}
        prioritized_ids: list[str] = []

        if len(running_task_ids) > 1:
            return False

        for task_id in running_task_ids:
            task = tasks_by_id.get(task_id)
            if task is None:
                continue
            if (
                task_id != state.active_task_id
                and not _is_stranded_commit_task(task)
                and not _should_requeue_commit_stage_task(task)
                and not has_stale_metadata
            ):
                continue
            if _is_stranded_commit_task(task):
                continue
            stale_pid = _subagent_process_is_stale(task)
            if _should_requeue_commit_stage_task(task):
                journal_message = _recover_existing_checkpoint_commit(root, task)
                if journal_message is not None:
                    journal_messages[task.id] = journal_message
                    record_recovery_report(
                        root,
                        task,
                        trigger="stale_runner_recovery",
                        stage="commit_to_git",
                        summary="Recovered existing checkpoint commit during stale runner recovery.",
                        runnable_state="runnable",
                        failure_classification="stale_runner",
                        actions=[
                            RecoveryAction(
                                action="clear_stale_active_state",
                                summary="Cleared stale active runner state for the task.",
                            ),
                            RecoveryAction(
                                action="finalize_existing_checkpoint",
                                summary="Recorded the existing checkpoint commit and finalized the task.",
                                metadata={"commit_sha": task.git.commit_sha},
                            ),
                        ],
                        warnings=["stale subagent pid detected"] if stale_pid else [],
                    )
                    if summary is not None and task.id not in summary.finalized_commit_task_ids:
                        summary.finalized_commit_task_ids.append(task.id)
                    if (
                        stale_pid
                        and summary is not None
                        and task.id not in summary.stale_process_task_ids
                    ):
                        summary.stale_process_task_ids.append(task.id)
                else:
                    journal_messages[task.id] = _recover_commit_task(root, task)
                    record_recovery_report(
                        root,
                        task,
                        trigger="stale_runner_recovery",
                        stage="commit_to_git",
                        summary="Recovered stale runner state and requeued commit_to_git.",
                        runnable_state="runnable",
                        failure_classification="stale_runner",
                        actions=[
                            RecoveryAction(
                                action="clear_stale_active_state",
                                summary="Cleared stale active runner state for the task.",
                            ),
                            RecoveryAction(
                                action="requeue_stage",
                                summary="Requeued the task at commit_to_git.",
                                metadata={"stage": "commit_to_git"},
                            ),
                        ],
                        warnings=["stale subagent pid detected"] if stale_pid else [],
                    )
                    if summary is not None and task.id not in summary.requeued_task_ids:
                        summary.requeued_task_ids.append(task.id)
                    prioritized_ids.append(task.id)
                if (
                    stale_pid
                    and summary is not None
                    and task.id not in summary.stale_process_task_ids
                ):
                    summary.stale_process_task_ids.append(task.id)
            elif _is_task_eligible_for_execution(task):
                _prepare_interrupted_task(
                    root,
                    task,
                    stage=task.pipeline_status,
                    summary=(
                        "Interrupted run recovered after stale runner detection. "
                        f"Resume from `{task.pipeline_status}`."
                    ),
                    reason=_stale_interruption_reason(
                        task, task.pipeline_status, stale_pid=stale_pid
                    ),
                )
                journal_messages[task.id] = interruption_journal_message(task)
                record_recovery_report(
                    root,
                    task,
                    trigger="stale_runner_recovery",
                    stage=task.pipeline_status,
                    summary=(
                        f"Recovered stale runner state and returned the task to `{task.pipeline_status}`."
                    ),
                    runnable_state="runnable",
                    failure_classification="stale_runner",
                    actions=[
                        RecoveryAction(
                            action="clear_stale_active_state",
                            summary="Cleared stale active runner state for the task.",
                        ),
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
                prioritized_ids.append(task.id)
                if (
                    stale_pid
                    and summary is not None
                    and task.id not in summary.stale_process_task_ids
                ):
                    summary.stale_process_task_ids.append(task.id)
            else:
                continue
            transitioned.append(task)
            mutated = True

        if transitioned:
            if state.active_task_id is not None:
                if summary is not None and summary.cleared_active_task_id is None:
                    summary.cleared_active_task_id = state.active_task_id
                state.active_task_id = None
            state.queue = [task_id for task_id in state.queue if task_id not in running_task_ids]
            requeued_ids = list(prioritized_ids)
            if requeued_ids:
                state.queue = [*requeued_ids, *state.queue]

        if state.active_task_id is not None and (
            state.active_task_id not in tasks_by_id or state.active_task_id in prioritized_ids
        ):
            if summary is not None and summary.cleared_active_task_id is None:
                summary.cleared_active_task_id = state.active_task_id
            state.active_task_id = None
            mutated = True

        commit_mutated = _recover_stranded_commit_tasks(root, state)
        if summary is not None and commit_mutated:
            refreshed_tasks = {task.id: task for task in list_tasks(root)}
            finalized_ids = [
                task_id
                for task_id, task in refreshed_tasks.items()
                if _is_stranded_commit_task(tasks_by_id.get(task_id, task))
                and task.runtime.execution_status == "done"
            ]
            for task_id in sorted(finalized_ids):
                if task_id not in summary.finalized_commit_task_ids:
                    summary.finalized_commit_task_ids.append(task_id)
            for task_id, task in refreshed_tasks.items():
                previous = tasks_by_id.get(task_id)
                if previous is None:
                    continue
                if (
                    _should_recover_flagged_commit_stage_task(root, previous)
                    and task.pipeline_status == "commit_to_git"
                    and task.status == "queued"
                    and task.id not in summary.requeued_task_ids
                ):
                    summary.requeued_task_ids.append(task.id)
                    continue
                if (
                    _is_stranded_commit_task(previous)
                    and not _is_stranded_commit_task(task)
                    and task.pipeline_status == "commit_to_git"
                    and task.id not in summary.requeued_task_ids
                ):
                    summary.requeued_task_ids.append(task.id)
        if transitioned:
            _persist_tasks_and_state_without_runner_guard(
                root,
                tasks=transitioned,
                state=state,
                journal_messages=journal_messages,
            )
        elif mutated:
            _save_state_without_runner_guard(root, state)
        return mutated or commit_mutated



def recover_stale_runner_state(root: Path) -> bool:
    return _recover_stale_runner_state(root)


def repair_workspace_state(root: Path) -> WorkspaceRepairSummary:
    from litehive.tasks.crud import list_tasks
    from .locking import _workspace_lock, workspace_mutation_guard
    from litehive.tasks.persistence import _save_state_without_runner_guard, load_state
    from litehive.tasks.queue_management import _enqueue_recovered_task
    from litehive.tasks.queue_ops import _is_task_eligible_for_execution, _restore_missing_queued_tasks
    from .workflow import _persist_tasks_and_state_without_runner_guard

    summary = WorkspaceRepairSummary()
    summary.stale_runner_recovered = _recover_stale_runner_state(root, summary=summary)
    summary.mutated = summary.stale_runner_recovered

    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        tasks_by_id = {task.id: task for task in list_tasks(root)}
        touched_tasks: list[TaskRecord] = []
        journal_messages: dict[str, str] = {}
        recovered_flagged_commit_ids: list[str] = []

        if state.active_task_id is not None and state.active_task_id not in tasks_by_id:
            summary.cleared_active_task_id = state.active_task_id
            state.active_task_id = None
            summary.mutated = True

        active_task = (
            tasks_by_id.get(state.active_task_id) if state.active_task_id is not None else None
        )
        if active_task is not None and active_task.runtime.execution_status != "running":
            state.active_task_id = None
            summary.cleared_active_task_id = active_task.id
            summary.mutated = True
            if _is_stranded_commit_task(active_task):
                journal_message = _recover_existing_checkpoint_commit(root, active_task)
                if journal_message is None:
                    journal_message = _recover_commit_task(root, active_task)
                    _enqueue_recovered_task(state, active_task.id)
                    if active_task.id not in summary.requeued_task_ids:
                        summary.requeued_task_ids.append(active_task.id)
                else:
                    state.queue = [item for item in state.queue if item != active_task.id]
                    if active_task.id not in summary.finalized_commit_task_ids:
                        summary.finalized_commit_task_ids.append(active_task.id)
                journal_messages[active_task.id] = journal_message
                touched_tasks.append(active_task)
            elif _should_requeue_commit_stage_task(active_task):
                _prepare_recovered_commit_task(active_task)
                _enqueue_recovered_task(state, active_task.id)
                journal_messages[active_task.id] = (
                    "Recovered interrupted `commit_to_git` attempt and requeued the task at "
                    "`commit_to_git`."
                )
                touched_tasks.append(active_task)
                if active_task.id not in summary.requeued_task_ids:
                    summary.requeued_task_ids.append(active_task.id)
            elif _is_task_eligible_for_execution(active_task):
                _prepare_interrupted_task_for_requeue(active_task)
                _enqueue_recovered_task(state, active_task.id)
                journal_messages[active_task.id] = (
                    "Recovered interrupted run and requeued the task at "
                    f"`{active_task.pipeline_status}`."
                )
                touched_tasks.append(active_task)
                if active_task.id not in summary.requeued_task_ids:
                    summary.requeued_task_ids.append(active_task.id)

        for task in tasks_by_id.values():
            if not _should_recover_flagged_commit_stage_task(root, task):
                continue
            journal_messages[task.id] = _recover_flagged_commit_task(task)
            touched_tasks.append(task)
            recovered_flagged_commit_ids.append(task.id)
            if task.id not in summary.requeued_task_ids:
                summary.requeued_task_ids.append(task.id)
            summary.mutated = True

        if recovered_flagged_commit_ids:
            state.queue = [
                task_id for task_id in state.queue if task_id not in recovered_flagged_commit_ids
            ]
            state.queue = [*recovered_flagged_commit_ids, *state.queue]

        seen: set[str] = set()
        normalized_queue: list[str] = []
        for task_id in state.queue:
            task = tasks_by_id.get(task_id)
            if task is None or not _is_task_eligible_for_execution(task):
                summary.removed_queue_entries.append(task_id)
                summary.mutated = True
                continue
            if task_id in seen:
                summary.deduped_queue_entries.append(task_id)
                summary.mutated = True
                continue
            seen.add(task_id)
            normalized_queue.append(task_id)
        state.queue = normalized_queue

        restored = _restore_missing_queued_tasks(state, tasks_by_id)
        if restored:
            summary.restored_queue_entries.extend(restored)
            summary.mutated = True

        if touched_tasks:
            _persist_tasks_and_state_without_runner_guard(
                root,
                tasks=touched_tasks,
                state=state,
                journal_messages=journal_messages,
            )
        elif summary.mutated:
            _save_state_without_runner_guard(root, state)
    return summary




def _reconcile_stale_runner_tasks(root: Path, state: WorkspaceState) -> bool:
    from litehive.tasks.crud import list_tasks
    from .locking import _runner_lock_is_held
    from litehive.tasks.queue_ops import _is_task_eligible_for_execution
    from .workflow import _persist_tasks_and_state_without_runner_guard

    tasks = list_tasks(root)
    tasks_by_id = {task.id: task for task in tasks}
    transitioned: list[TaskRecord] = []
    journal_messages: dict[str, str] = {}
    queue = list(state.queue)
    active_task = (
        tasks_by_id.get(state.active_task_id) if state.active_task_id is not None else None
    )

    if state.active_task_id is not None and active_task is None:
        state.active_task_id = None

    running_tasks = [t for t in tasks if t.runtime.execution_status == "running"]
    if len(running_tasks) > 1:
        return False

    for task in tasks:
        if task.runtime.execution_status != "running":
            continue
        if task.id == state.active_task_id:
            continue
        if _is_stranded_commit_task(task) or _should_requeue_commit_stage_task(task):
            queue = [item for item in queue if item != task.id]
            journal_messages[task.id] = _recover_existing_checkpoint_commit(
                root, task
            ) or _recover_commit_task(root, task)
            if task.status == "queued":
                queue.insert(0, task.id)
            transitioned.append(task)
            continue
        if _is_task_eligible_for_execution(task):
            _prepare_interrupted_task_for_requeue(task)
            queue = [item for item in queue if item != task.id]
            queue.insert(0, task.id)
            journal_messages[task.id] = (
                f"Reconciled stale runner state and requeued the task at `{task.pipeline_status}`."
            )
            transitioned.append(task)

    # Also reconcile the active task if runner is not live
    if state.active_task_id is not None:
        active_task = tasks_by_id.get(state.active_task_id)
        if (
            active_task is not None
            and active_task.runtime.execution_status == "running"
            and not _runner_lock_is_held(root)
        ):
            if _is_stranded_commit_task(active_task) or _should_requeue_commit_stage_task(
                active_task
            ):
                queue = [item for item in queue if item != active_task.id]
                journal_messages[active_task.id] = _recover_existing_checkpoint_commit(
                    root, active_task
                ) or _recover_commit_task(root, active_task)
                if active_task.status == "queued":
                    queue.insert(0, active_task.id)
                transitioned.append(active_task)
            elif _is_task_eligible_for_execution(active_task):
                _prepare_interrupted_task_for_requeue(active_task)
                queue = [item for item in queue if item != active_task.id]
                queue.insert(0, active_task.id)
                journal_messages[active_task.id] = (
                    f"Reconciled stale active task and requeued at `{active_task.pipeline_status}`."
                )
                transitioned.append(active_task)

            if active_task in transitioned:
                state.active_task_id = None

    if transitioned:
        state.queue = queue
        _persist_tasks_and_state_without_runner_guard(
            root,
            tasks=transitioned,
            state=state,
            journal_messages=journal_messages,
        )

    return bool(transitioned)

