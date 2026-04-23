"""Execution and launch recovery helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
from pathlib import Path
import shutil
import sqlite3
import subprocess

import yaml

from litehive.agents.session_store import (
    load_subagent_report,
    load_subagent_session,
    save_subagent_artifacts,
)
from litehive.config.loading import load_config
from litehive.config.paths import workspace_path
from litehive.config.registry import workspace_registry_path
from litehive.domain.common import utcnow
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.reports import RecoveryAction, TaskActivityEntry
from litehive.domain.runtime import (
    RuntimeContinuationHandoff,
    RuntimeInterruptionState,
    RuntimeSubagentState,
)
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.fs_cleanup import remove_tree_logged
from litehive.git.ops import GitError, is_git_repo, remove_worktree
from litehive.lifecycle.nodes.system import GitWorktreeSyncNode
from litehive.observability.events import last_event_timestamp
from litehive.state.locking import (
    clear_runner_lock_metadata,
    read_runner_lock_metadata,
    runner_lock_is_held,
    runner_lock_pid_is_stale,
)
from litehive.state.persist import load_state
from litehive.state.records import (
    clear_task_worktree_path,
    get_task_worktree_path,
    save_task,
    set_task_worktree_path,
)
from litehive.tasks.activity import append_task_activity
from litehive.tasks.audit import append_task_audit_entries, build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.paths import read_text_artifact, resolve_artifact_path, task_dir
from litehive.tasks.reports import record_recovery_report
from litehive.tasks.runtime import (
    apply_task_outcome,
    duration_seconds,
    finish_task_run_transition,
    summarize_transcript,
)
from litehive.worktree import (
    ensure_worktree_venv_link,
    resolve_recorded_worktree_path,
    serialize_worktree_path,
    task_worktree_branch,
    task_worktree_path,
)

from .detection import (
    LaunchFailure,
    TaskLaunchFailure,
    corrupt_task_launch_diagnostics,
    yaml_error_location,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LaunchRecoveryResult:
    fixed: bool
    summary: str
    actions: list[RecoveryAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocker: str | None = None


def prepare_task_launch(root: Path, task: TaskRecord) -> None:
    worktree = _ensure_task_worktree(root, task)
    _ensure_task_venv(root, task, worktree)
    _run_pre_stage_checks(root)


def attempt_launch_recovery(root: Path, task: TaskRecord, failure: LaunchFailure) -> LaunchRecoveryResult:
    actions: list[RecoveryAction] = []
    warnings: list[str] = []
    quarantine_requires_flag = False

    if failure.context in {"cycle_start_failed", "pre_stage_setup_failed"}:
        quarantine_requires_flag = _quarantine_corrupt_task_yaml(
            root,
            task,
            actions=actions,
            warnings=warnings,
        )
        _repair_workspace_registry(actions=actions, warnings=warnings)
        _repair_runner_lock(root, actions=actions, warnings=warnings)
        _repair_workspace_runtime(root, actions=actions, warnings=warnings)

    if failure.context == "worktree_setup_failed":
        _reset_task_worktree(root, task, actions=actions, warnings=warnings)

    if failure.context == "venv_sync_failed":
        _rebuild_task_venv(root, task, actions=actions, warnings=warnings)

    fixed = bool(actions) and not quarantine_requires_flag
    summary = (
        f"Launch recovery {failure.context} fixed: {failure.summary}"
        if fixed
        else f"Launch recovery {failure.context} failed: {failure.summary}"
    )
    blocker = None if fixed else failure.summary
    record_recovery_report(
        root,
        task,
        trigger_event_kind=TriggerEventKind.CRASH,
        origin_stage=_launch_origin_stage(task),
        summary=summary,
        runnable_state="runnable" if fixed else "blocked",
        failure_classification=failure.context,
        blocker=blocker,
        actions=actions,
        warnings=[
            *warnings,
            *[f"{key}: {value}" for key, value in sorted(failure.diagnostics.items()) if value not in (None, "", [], {})],
        ],
    )
    return LaunchRecoveryResult(
        fixed=fixed,
        summary=summary,
        actions=actions,
        warnings=warnings,
        blocker=blocker,
    )


def flag_task_after_failed_launch_recovery(root: Path, task: TaskRecord, failure: LaunchFailure) -> TaskRecord:
    before_task = snapshot_task_audit_state(task)
    reason = f"{failure.context}: {failure.summary}"
    origin_stage = _launch_origin_stage(task)
    task.status = "flagged"
    task.pipeline_status = "flagged"
    task.flag_reason = "recovery_failed"
    apply_task_outcome(
        task,
        kind="flagged",
        stage=origin_stage,
        reason_code="stage_exception",
        reason=reason,
        retry_count=task.runtime.retry_count,
        retry_limit=task.runtime.retry_limit,
        failure_classification=failure.context,
        failure_diagnostics=failure.diagnostics,
    )
    append_task_activity(
        root,
        task,
        TaskActivityEntry(
            role="recovery",
            stage=origin_stage,
            verdict="comment",
            message=f"Launch could not be recovered; task flagged.\ncontext: {failure.context}\nreason: {failure.summary}",
        ),
    )
    flagged = finish_task_run_transition(root, task, "flagged")
    append_task_audit_entries(
        root,
        [
            build_task_audit_entry(
                task_id=flagged.id,
                action="failed",
                actor="runner",
                source="launch_recovery",
                before_task=before_task,
                after_task=flagged,
                context={
                    "failure_context": failure.context,
                    "summary": failure.summary,
                    "origin_stage": origin_stage,
                },
            )
        ],
    )
    try:
        from litehive.lifecycle.persistence import SqlitePersistence

        SqlitePersistence(root).reset(task.id)
    except Exception:
        pass
    return flagged


def mark_interrupted_subagent(root: Path, task: TaskRecord, *, reason: str, stage: str) -> RuntimeSubagentState | None:
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


def recover_stale_runner_state(
    root: Path,
    *,
    summary: WorkspaceRepairSummary | None = None,
) -> bool:
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
        tasks = list_tasks(root)
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


def _launch_origin_stage(task: TaskRecord) -> str:
    stage = task.pipeline_status
    if stage in {"backlog", "flagged", "", None}:
        return implementation_entry_stage(task)
    return stage


def _ensure_task_worktree(root: Path, task: TaskRecord) -> Path:
    recorded = resolve_recorded_worktree_path(root, get_task_worktree_path(task))
    if recorded is not None and recorded.exists():
        return recorded
    if not is_git_repo(root):
        return root

    branch = task_worktree_branch(task)
    existing = GitWorktreeSyncNode._registered_worktree_for_branch(root, branch)
    if existing is not None:
        set_task_worktree_path(task, serialize_worktree_path(existing))
        save_task(root, task)
        return existing

    worktree = task_worktree_path(root, task)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    try:
        GitWorktreeSyncNode._prune_stale_worktrees(root)
    except GitError as exc:
        raise TaskLaunchFailure(
            context="worktree_setup_failed",
            summary=str(exc),
            diagnostics={"branch": branch, "worktree_path": str(worktree)},
        ) from exc

    created = subprocess.run(
        ["git", "worktree", "add", "--force", "-B", branch, str(worktree), "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        raise TaskLaunchFailure(
            context="worktree_setup_failed",
            summary=created.stderr.strip() or created.stdout.strip() or "git worktree add failed",
            diagnostics={"branch": branch, "worktree_path": str(worktree)},
        )

    ensure_worktree_venv_link(root, worktree)
    set_task_worktree_path(task, serialize_worktree_path(worktree))
    save_task(root, task)
    return worktree


def _ensure_task_venv(root: Path, task: TaskRecord, worktree: Path) -> None:
    del root, task
    if not (worktree / "pyproject.toml").exists():
        return
    uv = shutil.which("uv")
    if uv is None:
        raise TaskLaunchFailure(
            context="venv_sync_failed",
            summary="uv executable is not available for task launch",
            diagnostics={"worktree_path": str(worktree)},
        )
    sync = subprocess.run(
        [uv, "sync", "--extra", "dev"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    if sync.returncode != 0:
        raise TaskLaunchFailure(
            context="venv_sync_failed",
            summary=f"uv sync failed in {worktree}",
            diagnostics={
                "worktree_path": str(worktree),
                "stderr": sync.stderr.strip() or sync.stdout.strip() or "uv sync failed",
            },
        )


def _run_pre_stage_checks(root: Path) -> None:
    try:
        load_config(root)
        load_state(root)
        read_runner_lock_metadata(root)
    except Exception as exc:
        raise TaskLaunchFailure(
            context="pre_stage_setup_failed",
            summary=f"pre-stage setup failed: {exc}",
        ) from exc


def _quarantine_corrupt_task_yaml(
    root: Path,
    task: TaskRecord,
    *,
    actions: list[RecoveryAction],
    warnings: list[str],
) -> bool:
    diagnostics = corrupt_task_launch_diagnostics(root, task.id)
    task_yaml_path = diagnostics.get("task_yaml_path")
    if not task_yaml_path:
        return False
    path = Path(task_yaml_path)
    if not path.exists():
        return True
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.corrupt-{timestamp}")
    try:
        path.replace(backup)
    except OSError as exc:
        warnings.append(f"failed to quarantine corrupt task.yaml {path}: {exc}")
        return False
    actions.append(
        RecoveryAction(
            action="quarantine_corrupt_task_yaml",
            summary=f"Moved corrupt task.yaml aside to {backup}.",
            metadata={
                "task_id": task.id,
                "path": str(path),
                "backup_path": str(backup),
                "error": diagnostics.get("task_yaml_error", ""),
            },
        )
    )
    return True


def _repair_workspace_registry(*, actions: list[RecoveryAction], warnings: list[str]) -> None:
    path = workspace_registry_path()
    if not path.exists():
        return
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
        return
    except yaml.YAMLError as exc:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.corrupt-{timestamp}")
        path.replace(backup)
        actions.append(
            RecoveryAction(
                action="quarantine_corrupt_workspaces_registry",
                summary=f"Moved corrupt workspace registry aside to {backup}.",
                metadata={"path": str(path), "backup_path": str(backup)},
            )
        )
        warnings.append(f"workspaces.yaml was corrupt ({yaml_error_location(exc)})")
    except OSError as exc:
        warnings.append(f"failed to inspect workspaces.yaml: {exc}")


def _repair_runner_lock(root: Path, *, actions: list[RecoveryAction], warnings: list[str]) -> None:
    lock_path = workspace_path(root, "runtime", ".runner.lock")
    if not lock_path.exists():
        return

    try:
        yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        if runner_lock_is_held(root):
            warnings.append(f"runner lock is corrupt but still held: {exc}")
            return
        try:
            lock_path.unlink()
        except OSError as unlink_exc:
            warnings.append(f"failed to remove corrupt runner lock: {unlink_exc}")
            return
        actions.append(
            RecoveryAction(
                action="remove_corrupt_runner_lock",
                summary=f"Removed corrupt runner lock {lock_path}.",
                metadata={"path": str(lock_path)},
            )
        )
        return

    if runner_lock_pid_is_stale(root) and not runner_lock_is_held(root):
        clear_runner_lock_metadata(root)
        actions.append(
            RecoveryAction(
                action="clear_stale_runner_lock",
                summary=f"Cleared stale runner lock metadata at {lock_path}.",
                metadata={"path": str(lock_path)},
            )
        )


def _repair_workspace_runtime(root: Path, *, actions: list[RecoveryAction], warnings: list[str]) -> None:
    from .workspace_repair import repair_workspace_state

    try:
        summary = repair_workspace_state(root, repair_broken_venvs_in_checkouts=True)
    except Exception as exc:
        warnings.append(f"workspace repair failed: {exc}")
        return
    if not summary.mutated and not summary.broken_venv_binaries:
        return
    metadata = {
        "stale_runner_recovered": summary.stale_runner_recovered,
        "cleared_active_task_id": summary.cleared_active_task_id,
        "requeued_task_ids": summary.requeued_task_ids,
        "stale_process_task_ids": summary.stale_process_task_ids,
        "broken_venv_binaries": summary.broken_venv_binaries,
    }
    action = RecoveryAction(
        action="repair_workspace_runtime",
        summary="Repaired workspace runtime state.",
        metadata=metadata,
    )
    actions.append(action)
    if summary.broken_venv_binaries:
        warnings.append(
            "broken checkout venv binaries remain: " + ", ".join(summary.broken_venv_binaries)
        )


def _reset_task_worktree(
    root: Path,
    task: TaskRecord,
    *,
    actions: list[RecoveryAction],
    warnings: list[str],
) -> None:
    recorded = resolve_recorded_worktree_path(root, get_task_worktree_path(task))
    if recorded is None:
        return

    logger.info("Deleting stale task worktree directory %s", recorded)
    try:
        remove_worktree(root, recorded)
    except GitError as exc:
        warnings.append(f"failed to remove registered worktree {recorded}: {exc}")
    remove_tree_logged(recorded, logger=logger, target_label="stale task worktree directory")
    clear_task_worktree_path(task)
    save_task(root, task)
    actions.append(
        RecoveryAction(
            action="reset_task_worktree",
            summary=f"Removed stale task worktree at {recorded}.",
            metadata={"task_id": task.id, "worktree_path": str(recorded)},
        )
    )


def _rebuild_task_venv(
    root: Path,
    task: TaskRecord,
    *,
    actions: list[RecoveryAction],
    warnings: list[str],
) -> None:
    recorded = resolve_recorded_worktree_path(root, get_task_worktree_path(task))
    if recorded is None:
        warnings.append("task worktree path missing while rebuilding venv")
        return
    venv_path = recorded / ".venv"
    if venv_path.exists() or venv_path.is_symlink():
        remove_tree_logged(venv_path, logger=logger, target_label="broken task venv")
    if not shutil.which("uv"):
        warnings.append("uv executable unavailable for task venv rebuild")
        return
    sync = subprocess.run(
        ["uv", "sync", "--extra", "dev"],
        cwd=str(recorded),
        capture_output=True,
        text=True,
        check=False,
    )
    if sync.returncode != 0:
        warnings.append(sync.stderr.strip() or sync.stdout.strip() or "uv sync failed during rebuild")
        return
    actions.append(
        RecoveryAction(
            action="rebuild_task_venv",
            summary=f"Rebuilt task venv in {recorded}.",
            metadata={"task_id": task.id, "worktree_path": str(recorded)},
        )
    )


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


def _should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    return task.pipeline_status == "commit_to_git" and task.status in {"queued", "in_progress", "interrupted"}


def _has_inactive_running_tasks(
    root: Path,
    tasks_by_id: dict[str, TaskRecord],
    timeout_seconds: float,
) -> bool:
    for task in tasks_by_id.values():
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
        if (datetime.now(UTC) - event_time).total_seconds() > timeout_seconds:
            return True
    return False


def _canonicalize_resumable_task(task: TaskRecord, *, stage: str) -> None:
    from litehive.tasks.queue import canonicalize_resumable_queue_task

    canonicalize_resumable_queue_task(task, stage=stage)


def _interrupted_subagent_snippet(root: Path, task: TaskRecord, active: RuntimeSubagentState) -> str:
    subagent_base = task_dir(root, task) / active.path
    report = load_subagent_report(root, task.id, active.id)
    if report:
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
    report_payload["summary"] = report_payload.get("summary") or subagent.transcript_snippet
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
        stage=stage,
        kind="restart",
        reason=reason,
        from_engine=None if interrupted_subagent is None else interrupted_subagent.engine,
        to_engine=None if interrupted_subagent is None else interrupted_subagent.engine,
        subagent_id=None if interrupted_subagent is None else interrupted_subagent.id,
        subagent_path=None if interrupted_subagent is None else interrupted_subagent.path,
        status=None if interrupted_subagent is None else interrupted_subagent.status,
        summary=summary,
        transcript_snippet="" if interrupted_subagent is None else interrupted_subagent.transcript_snippet,
        warnings=[],
        session_path=None,
        report_path=None,
        transcript_path=None if interrupted_subagent is None else f"{interrupted_subagent.path}/transcript.md",
        continuation=None if interrupted_subagent is None else interrupted_subagent.continuation,
        updated_at=now,
    )
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "stage": stage,
            "status": "interrupted",
            "started_at": started_at,
            "completed_at": now,
            "updated_at": now,
            "duration_seconds": duration_seconds(started_at, now),
            "verdict": "blocked",
            "summary": summary,
        }
    )


def _can_attempt_stale_runner_recovery(
    root: Path,
    tasks_by_id: dict[str, TaskRecord],
    running_task_ids: list[str],
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
            RecoveryAction(action="clear_stale_active_state", summary="Cleared stale active runner state for the task."),
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
    from litehive.state.locking import subagent_process_is_stale
    from litehive.tasks.queue import is_task_eligible_for_execution

    if not is_task_eligible_for_execution(task):
        return False, None, False
    stale_pid = subagent_process_is_stale(task)
    stage = task.pipeline_status
    prepare_interrupted_task(
        root,
        task,
        stage=stage,
        summary=f"Interrupted run recovered after stale runner detection. Resume from `{stage}`.",
        reason=stale_interruption_reason(task, stage, stale_pid=stale_pid),
    )
    _canonicalize_resumable_task(task, stage=stage)
    _record_stale_recovery(
        root,
        task,
        stage=stage,
        journal_message=f"Recovered stale runner state and returned the task to `{stage}`.",
        summary=summary,
        stale_pid=stale_pid,
    )
    return True, interruption_journal_message(task), True


def _is_stranded_commit_task(task: TaskRecord) -> bool:
    return task.pipeline_status == "done" and task.git.commit_sha is None and task.git.checkpoint_attempts > 0


should_requeue_commit_stage_task = _should_requeue_commit_stage_task
is_stranded_commit_task = _is_stranded_commit_task


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
    from litehive.state.locking import read_runner_lock_metadata, runner_metadata_present

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
    from litehive.tasks.queue import (
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
        if task.runtime.execution_status == "running":
            continue
        if task.status not in {"queued", "in_progress", "interrupted"}:
            continue
        if task.status != "interrupted" and not is_task_eligible_for_execution(task):
            continue
        if task.status == "queued" and task.id != state.active_task_id and not task_has_resume_marker(task):
            continue
        stage = resumable_queue_stage(task)
        if stage is None:
            continue
        queue_contains_task = task.id in state.queue
        queue_index = None if not queue_contains_task else state.queue.index(task.id)
        should_normalize = (
            task.status != "queued"
            or task.runtime.execution_status != "idle"
            or task.pipeline_status != stage
            or task.runtime.current_stage.stage != stage
            or task.runtime.current_stage.status != "idle"
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
    from litehive.db.schema import connect_workspace_db

    with connect_workspace_db(root) as connection:
        try:
            row = connection.execute(
                """
                SELECT 1
                FROM task_state
                WHERE (
                    json_extract(payload, '$.status') = 'in_progress'
                    AND json_extract(payload, '$.runtime.execution_status') != 'running'
                    AND json_extract(payload, '$.pipeline_status') IN (
                        'grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'merge_failed'
                    )
                ) OR (
                    json_extract(payload, '$.status') = 'interrupted'
                    AND COALESCE(
                        json_extract(payload, '$.runtime.interruption.resume_stage'),
                        json_extract(payload, '$.runtime.interruption.pipeline_status'),
                        json_extract(payload, '$.pipeline_status')
                    ) IN ('grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'merge_failed')
                ) OR (
                    json_extract(payload, '$.status') = 'queued'
                    AND json_extract(payload, '$.pipeline_status') IN (
                        'grooming', 'implementing', 'testing', 'accepting', 'commit_to_git', 'merge_failed'
                    )
                    AND (
                        (
                            json_extract(payload, '$.runtime.current_stage.stage')
                            = json_extract(payload, '$.pipeline_status')
                            AND json_extract(payload, '$.runtime.current_stage.status') IN (
                                'idle', 'paused', 'interrupted'
                            )
                        )
                        OR json_extract(payload, '$.runtime.interruption.resume_stage')
                            = json_extract(payload, '$.pipeline_status')
                        OR json_extract(payload, '$.runtime.interruption.pipeline_status')
                            = json_extract(payload, '$.pipeline_status')
                        OR json_extract(payload, '$.runtime.continuation_handoff.stage')
                            = json_extract(payload, '$.pipeline_status')
                    )
                )
                LIMIT 1
                """
            ).fetchone()
        except sqlite3.OperationalError:
            return False
    return row is not None


def _update_active_task_after_recovery(
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
    should_clear_active_task_id = (
        state.active_task_id not in tasks_by_id
        or state.active_task_id in prioritized_ids
        or (
            active_task is not None
            and active_task.runtime.execution_status != "running"
            and active_task.id not in running_task_ids
            and not is_stranded_commit_task(active_task)
            and not should_requeue_commit_stage_task(active_task)
        )
    )
    if should_clear_active_task_id:
        _record_cleared_active_task(summary, active_task, state.active_task_id)
        state.active_task_id = None
        mutated = True
    return mutated


def _record_cleared_active_task(
    summary: WorkspaceRepairSummary | None,
    active_task: TaskRecord | None,
    active_task_id: str,
) -> None:
    if summary is None or summary.cleared_active_task_id is not None:
        return
    if active_task is not None and (
        is_stranded_commit_task(active_task) or should_requeue_commit_stage_task(active_task)
    ):
        return
    summary.cleared_active_task_id = active_task_id
