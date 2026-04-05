"""Recovery agent logic: stage recovery, commit recovery, rollback, and recover."""

import hashlib
import re
from pathlib import Path

from litehive.config import LitehiveConfig
from litehive.git_ops import (
    GitError,
    abort_revert,
    commit_task,
    has_changes,
    rollback_message,
    rollback_task,
    current_head,
)
from litehive.models import RecoveryAction, StageReport, TaskRecord
from litehive.subagents import SubagentManager, stage_report_from_subagent
from litehive.tasks import (
    _atomic_write_text,
    _workspace_lock,
    append_journal,
    collect_recovery_evidence,
    implementation_entry_stage,
    load_state,
    persist_task_and_state,
    prepare_completed_task_for_recovery,
    record_recovery_report,
    save_task,
    state_path,
    task_dir,
    task_file,
    task_runtime_file,
    workspace_mutation_guard,
)

from ._models import resolve_model
from ._types import RollbackSummary, _path_within


def _traceback_text(failed_report: StageReport) -> str:
    traceback_text = failed_report.failure_diagnostics.get("traceback")
    if isinstance(traceback_text, str) and traceback_text.strip():
        return traceback_text
    feedback = failed_report.feedback or ""
    return feedback if "Traceback" in feedback else ""


def _traceback_frame_paths(traceback_text: str) -> list[Path]:
    return [Path(match) for match in re.findall(r'File "([^"]+)"', traceback_text)]


def _traceback_fingerprint(traceback_text: str, summary: str) -> str:
    signature_lines = [
        line.strip()
        for line in traceback_text.splitlines()
        if line.strip().startswith('File "') or line.strip().startswith(("raise ", "AssertionError", "RuntimeError", "ValueError", "TypeError"))
    ]
    payload = "\n".join(signature_lines) or summary
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _classify_recovery_failure_owner(
    root: Path,
    failed_report: StageReport,
    *,
    config: LitehiveConfig | None,
) -> tuple[str, str, Path | None]:
    traceback_text = _traceback_text(failed_report)
    if not traceback_text:
        return "unknown", "", None
    frame_paths = _traceback_frame_paths(traceback_text)
    source_root = None
    if config and config.litehive_source_path:
        source_root = Path(config.litehive_source_path).expanduser().resolve()
    for frame in frame_paths:
        if source_root is not None and _path_within(frame, source_root):
            return "litehive", traceback_text, source_root
        if _path_within(frame, root):
            return "project", traceback_text, source_root
        normalized = frame.as_posix()
        if "/site-packages/litehive/" in normalized or normalized.endswith("/litehive/__init__.py") or "/litehive/" in normalized:
            return "litehive", traceback_text, source_root
    return "unknown", traceback_text, source_root


def _attempt_stage_recovery(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    step: str,
    failed_report: StageReport,
    *,
    subagents: SubagentManager | None = None,
    config: LitehiveConfig | None = None,
    role_name: str = "swe",
    engine_name: str = "codex",
    model_name: str | None = None,
) -> StageReport | None:
    """Launch a recovery agent after a stage failure. Returns a new report or None."""
    if subagents is None:
        return None

    evidence = collect_recovery_evidence(root, task, stage=step)
    evidence_lines = "\n".join(
        f"- {item.label}: {item.path or 'n/a'} ({'present' if item.exists else 'missing'}) :: {item.summary}"
        for item in evidence
    )
    failure_owner, traceback_text, source_root = _classify_recovery_failure_owner(
        root,
        failed_report,
        config=config,
    )
    append_journal(
        root, task,
        f"Stage `{step}` {failed_report.verdict}: {failed_report.summary}. Launching recovery agent.",
    )
    prompt = (
        f"You are running as Litehive's recovery agent for task {task.id} ({task.title}).\n\n"
        f"Failure trigger: stage `{step}` ended with verdict `{failed_report.verdict}`.\n"
        f"Failure summary: {failed_report.summary}\n\n"
        f"Failure ownership classification: {failure_owner}\n\n"
        f"Previous report feedback:\n{failed_report.feedback or '(none)'}\n\n"
        f"Working directory: {execution_root}\n\n"
        f"Recovery evidence gathered automatically:\n{evidence_lines}\n\n"
        f"Bounded recovery policy:\n"
        f"- gather enough evidence to classify the failure\n"
        f"- apply only the smallest safe repair needed to restore a runnable path\n"
        f"- prefer fixing continuation state, engine bindings, prompts, or task-local state over broad refactors\n"
        f"- if the task is underspecified, leave explicit notes and keep it runnable for planner/grooming\n\n"
        f"Acceptance criteria:\n"
        + "\n".join(f"- {c}" for c in task.acceptance_criteria)
        + f"\n\nWhen you finish, submit a detailed report with `litehive report`.\n"
        f"Use verdict `pass` only if the task is runnable again or the current stage is now complete.\n"
        f"Use verdict `blocked` or `fail` if a blocker remains.\n"
    )

    recovery_result = subagents.run(
        task,
        role="recovery",
        engine_name=engine_name,
        prompt=prompt,
        model=model_name,
    )

    from litehive.tasks import load_task_thread

    thread = load_task_thread(root, task)
    recovery_comments = [
        c for c in thread
        if c.step == step and c.verdict in ("pass", "accept")
    ]
    if recovery_comments:
        latest = recovery_comments[-1]
        record_recovery_report(
            root,
            task,
            trigger="stage_failure",
            stage=step,
            summary=latest.message.splitlines()[0] if latest.message else f"{step} recovered",
            runnable_state="runnable",
            failure_classification=failed_report.failure_classification or failed_report.verdict,
            actions=[
                RecoveryAction(
                    action="resume_current_stage",
                    summary=f"Recovery agent repaired the task and returned `{step}` to a runnable state.",
                    metadata={"verdict": latest.verdict},
                )
            ],
            warnings=list(failed_report.warnings),
            recovery_subagent_id=recovery_result.ref.id,
            recovery_subagent_path=recovery_result.ref.path,
        )
        append_journal(root, task, f"Recovery agent resolved {step}: {latest.verdict}")
        return StageReport(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            verdict=latest.verdict,  # type: ignore[arg-type]
            summary=latest.message.splitlines()[0] if latest.message else f"{step} recovered",
            feedback=latest.message,
            files_changed=latest.files_changed,
        )

    recovery_report = stage_report_from_subagent(task, step, recovery_result, root=root)
    if recovery_report.verdict in ("pass", "accept"):
        record_recovery_report(
            root,
            task,
            trigger="stage_failure",
            stage=step,
            summary=recovery_report.summary,
            runnable_state="runnable",
            failure_classification=failed_report.failure_classification or failed_report.verdict,
            actions=[
                RecoveryAction(
                    action="resume_current_stage",
                    summary=f"Recovery agent repaired the task and returned `{step}` to a runnable state.",
                    metadata={"verdict": recovery_report.verdict},
                )
            ],
            warnings=[*failed_report.warnings, *recovery_report.warnings],
            recovery_subagent_id=recovery_result.ref.id,
            recovery_subagent_path=recovery_result.ref.path,
        )
        append_journal(root, task, f"Recovery agent resolved {step}: {recovery_report.verdict}")
        return recovery_report

    blocker = recovery_report.summary or failed_report.summary
    record_recovery_report(
        root,
        task,
        trigger="stage_failure",
        stage=step,
        summary=f"Recovery agent could not restore `{step}` to a runnable state.",
        runnable_state="blocked",
        failure_classification=failed_report.failure_classification or failed_report.verdict,
        blocker=blocker,
        actions=[
            RecoveryAction(
                action="no_safe_repair",
                applied=False,
                summary="Recovery agent investigated the failure but could not apply a safe bounded repair.",
            )
        ],
        warnings=[*failed_report.warnings, *recovery_report.warnings],
        recovery_subagent_id=recovery_result.ref.id,
        recovery_subagent_path=recovery_result.ref.path,
    )
    append_journal(root, task, f"Recovery agent could not resolve {step}.")
    return None


def _resolve_recovery_engine(
    task: TaskRecord,
    config: "LitehiveConfig | None",
) -> tuple[str, str | None]:
    if config and config.recovery_engine:
        engine = config.recovery_engine
    else:
        engine = task.engine or (config.default_engine if config else "codex")
    model = resolve_model(task, config, engine_name=engine) if config else None
    return engine, model


def _attempt_commit_recovery(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    reason: str,
    *,
    subagents: SubagentManager | None = None,
    config: "LitehiveConfig | None" = None,
) -> str | None:
    """Launch a recovery agent after a commit_to_git failure.

    Writes a structured recovery report and returns the current HEAD SHA when
    the agent succeeds (exit_code == 0), or ``None`` if recovery failed or no
    subagents manager was provided.
    """
    if subagents is None:
        return None

    engine_name, model = _resolve_recovery_engine(task, config)
    prompt = (
        f"CommitToGit failed: {reason}\n"
        f"Investigate and fix the commit failure, then complete the commit."
    )
    recovery_result = subagents.run(
        task,
        role="recovery",
        engine_name=engine_name,
        model=model,
        prompt=prompt,
    )
    subagent_id = recovery_result.ref.id if recovery_result and recovery_result.ref else None
    subagent_path = recovery_result.ref.path if recovery_result and recovery_result.ref else None

    record_recovery_report(
        root,
        task,
        trigger="commit_to_git_failure",
        stage="commit_to_git",
        summary=f"Recovery agent launched for commit failure: {reason}",
        runnable_state="runnable",
        actions=[
            RecoveryAction(
                action="recover_commit_to_git",
                summary="Ran recovery agent to fix commit failure.",
            )
        ],
        recovery_subagent_id=subagent_id,
        recovery_subagent_path=subagent_path,
    )

    if recovery_result and recovery_result.exit_code == 0:
        return current_head(root) or "recovered"
    return None


def _require_completed_task(task: TaskRecord, action: str) -> None:
    if task.status != "done" or task.pipeline_status != "done":
        raise GitError(f"Task {task.id} is not completed; cannot {action}")


def _capture_persisted_files(paths: list[Path]) -> dict[Path, str | None]:
    return {
        path: path.read_text(encoding="utf-8") if path.exists() else None
        for path in paths
    }


def _restore_persisted_files(snapshot: dict[Path, str | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            if path.exists():
                path.unlink()
            continue
        _atomic_write_text(path, content)


def rollback_completed_task(root: Path, task_id: str) -> RollbackSummary:
    root = root.resolve()
    with workspace_mutation_guard(root), _workspace_lock(root):
        from litehive.tasks import get_task
        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        _require_completed_task(task, action="rollback")

        attempt = task.git.checkpoint_attempts
        recovery_stage = implementation_entry_stage(task)
        state = load_state(root)
        snapshot = _capture_persisted_files(
            [
                task_file(root, task),
                task_runtime_file(root, task),
                state_path(root),
                task_dir(root, task) / "journal.md",
            ]
        )
        rollback = None
        try:
            rollback = rollback_task(root, task)
            prepare_completed_task_for_recovery(task, recovery_stage=recovery_stage)
            task.git.rolled_back_checkpoint_attempt = attempt
            state.active_task_id = None
            state.queue = [item for item in state.queue if item != task.id]
            state.queue.append(task.id)
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=(
                    "Checkpoint rollback requested.\n"
                    f"- rolled_back_attempt: `{attempt}`\n"
                    f"- recovery_stage: `{recovery_stage}`"
                ),
            )
            rollback_checkpoint = commit_task(root, rollback_message(task, attempt))
            if rollback_checkpoint is None:
                raise GitError("git rollback commit failed")
        except Exception:
            if rollback is not None and has_changes(root):
                abort_revert(root)
            _restore_persisted_files(snapshot)
            raise
        return RollbackSummary(
            task=task,
            rollback_sha=rollback_checkpoint.commit_sha,
            rolled_back_sha=rollback.rolled_back_sha,
        )


def recover_completed_task(root: Path, task_id: str) -> TaskRecord:
    root = root.resolve()
    with workspace_mutation_guard(root), _workspace_lock(root):
        from litehive.tasks import get_task
        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        _require_completed_task(task, action="recover")

        recovery_stage = implementation_entry_stage(task)
        prepare_completed_task_for_recovery(task, recovery_stage=recovery_stage)
        state = load_state(root)
        state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        state.queue.append(task.id)
        persist_task_and_state(
            root,
            task=task,
            state=state,
            journal_message="Task recovered for another implementation pass.",
        )
        return task
