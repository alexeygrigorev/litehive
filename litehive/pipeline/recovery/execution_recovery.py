"""Execution recovery: recovery agents, rollback, and recover operations."""

from __future__ import annotations

from pathlib import Path

from litehive.agents import SubagentManager, stage_report_from_subagent
from litehive.config import LitehiveConfig, state_path
from litehive.git import (
    GitError,
    abort_revert,
    commit_task,
    current_head,
    has_changes,
    rollback_message,
    rollback_task,
)
from litehive.models import RecoveryAction, StageReport, TaskRecord
from litehive.tasks.crud import save_task_runtime
from litehive.tasks.journal import append_journal
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.paths import task_dir, task_file, task_runtime_file
from litehive.tasks.persistence import _atomic_write_text, load_state
from litehive.tasks.queue_management import prepare_completed_task_for_recovery
from litehive.tasks.reports import (
    collect_recovery_evidence,
    load_task_thread,
    record_recovery_report,
)
from litehive.workspace.locking import _workspace_lock, workspace_mutation_guard
from litehive.workspace.workflow import persist_task_and_state

from .detection import (
    _classify_recovery_failure_owner,
    _load_failed_subagent_diagnostics,
    _resolve_recovery_execution_root,
    _traceback_fingerprint,
)


def _recovery_attempt_exhausted(root: Path, task: TaskRecord, step: str) -> bool:
    max_recovery_attempts = 1
    recovery_count = sum(1 for sa in task.subagents if sa.role == "recovery")
    if recovery_count < max_recovery_attempts:
        return False
    append_journal(
        root,
        task,
        f"[recovery] Skipping recovery for `{step}`: {recovery_count} recovery attempts exhausted (limit: {max_recovery_attempts}).",
    )
    return True


def _recovery_evidence_lines(root: Path, task: TaskRecord, *, stage: str) -> str:
    evidence = collect_recovery_evidence(root, task, stage=stage)
    return "\n".join(
        f"- {item.label}: {item.path or 'n/a'} ({'present' if item.exists else 'missing'}) :: {item.summary}"
        for item in evidence
    )


def _record_missing_litehive_source_repo(
    root: Path,
    task: TaskRecord,
    *,
    step: str,
    failed_report: StageReport,
    failure_owner: str,
    report_attempt_summary: str,
) -> None:
    blocker = (
        "Recovery requires a Litehive source checkout, but neither `litehive_source_path` nor the "
        "current workspace root points to a Litehive repo."
    )
    record_recovery_report(
        root,
        task,
        trigger="stage_failure",
        stage=step,
        summary="Recovery could not start because no Litehive source repo was available.",
        runnable_state="blocked",
        failure_classification=failure_owner,
        blocker=blocker,
        actions=[
            RecoveryAction(
                action="missing_litehive_source_repo",
                applied=False,
                summary=blocker,
            )
        ],
        warnings=[*failed_report.warnings, report_attempt_summary],
    )
    append_journal(root, task, blocker)


def _build_standard_recovery_prompt(
    task: TaskRecord,
    *,
    step: str,
    failed_report: StageReport,
    failure_owner: str,
    recovery_root: Path,
    evidence_lines: str,
    diagnostics_text: str,
) -> str:
    return (
        f"You are running as Litehive's recovery agent for task {task.id} ({task.title}).\n\n"
        f"Your job is to diagnose why the previous agent failed and fix Litehive infrastructure if that is the root cause.\n"
        f"Do NOT redo the failed `{step}` work, do NOT run the task's normal verification loop, and do NOT submit `{step}`'s verdict for the prior agent.\n\n"
        f"Failure trigger: stage `{step}` ended with verdict `{failed_report.verdict}`.\n"
        f"Failure summary: {failed_report.summary}\n\n"
        f"Failure ownership classification: {failure_owner}\n\n"
        f"Previous report feedback:\n{failed_report.feedback or '(none)'}\n\n"
        f"Recovery working directory: {recovery_root}\n"
        f"This is the Litehive source repo. Fix Litehive here if the failure came from Litehive infrastructure.\n\n"
        f"Recovery evidence gathered automatically:\n{evidence_lines}\n\n"
        f"Failed subagent diagnostics:\n{diagnostics_text}\n\n"
        f"Diagnosis checklist:\n"
        f"- Did the failed agent produce output in stdout/stderr/transcript?\n"
        f"- Did it try to call `litehive report`? What exact error did that attempt get?\n"
        f"- What does session metadata say about exit code, interruption, and state?\n"
        f"- What is the Litehive root cause in prompts, resume/report wiring, runtime, or adapter code?\n"
        f"- What is the smallest safe Litehive fix that lets `{step}` be retried cleanly?\n\n"
        f"Bounded recovery policy:\n"
        f"- gather enough evidence to classify the failure\n"
        f"- apply only the smallest safe Litehive repair needed to restore a runnable path\n"
        f"- prefer fixing report wiring, continuation state, engine bindings, prompts, and runtime bugs over broad refactors\n"
        f"- If the failure is a task/project bug rather than a Litehive bug, do not implement the task; explain that in your recovery verdict\n\n"
        f"Examples:\n"
        f"- DO: inspect stdout/stderr/session, find that `litehive report` targeted the wrong task id, fix Litehive, and report the recovery fix.\n"
        f"- DO: inspect resume/session artifacts, find that Codex resume handling is broken, patch Litehive, and report the recovery fix.\n"
        f"- DO NOT: re-run the failed stage's tests just to finish the task.\n"
        f"- DO NOT: edit project/task code to make the original implementation pass.\n"
        f"- DO NOT: submit the failed stage verdict as if you were the previous agent.\n\n"
        f"Acceptance criteria:\n"
        + "\n".join(f"- {c}" for c in task.acceptance_criteria)
        + "\n\nWhen you finish, submit your own detailed recovery report with `litehive report`.\n"
        f"Use verdict `pass` only if you fixed a Litehive issue and `{step}` should now be retried with the fix in place.\n"
        f"Use verdict `blocked` or `fail` if a blocker remains or no Litehive fix was possible.\n"
    )


def _build_self_heal_prompt(
    task: TaskRecord,
    *,
    step: str,
    failed_report: StageReport,
    traceback_text: str,
    source_root: Path,
    evidence_lines: str,
    diagnostics_text: str,
) -> str:
    return (
        f"You are running as Litehive's SELF-HEAL recovery agent.\n\n"
        f"A litehive bug caused task {task.id} ({task.title}) to fail during stage `{step}`.\n\n"
        f"Your job is to diagnose why the previous agent failed, fix the Litehive bug, and report the recovery fix.\n"
        f"Do NOT redo the failed `{step}` work, do NOT run the task's normal verification stage for it, and do NOT submit `{step}`'s verdict on the prior agent's behalf.\n\n"
        f"Failure summary: {failed_report.summary}\n\n"
        f"Traceback:\n{traceback_text}\n\n"
        f"Previous report feedback:\n{failed_report.feedback or '(none)'}\n\n"
        f"Recovery evidence:\n{evidence_lines}\n\n"
        f"Failed subagent diagnostics:\n{diagnostics_text}\n\n"
        f"IMPORTANT: This failure is in litehive's own code, NOT in the external project.\n"
        f"Your working directory is the litehive source tree: {source_root}\n\n"
        f"Diagnosis checklist:\n"
        f"- Did the failed agent produce stdout/stderr/transcript output?\n"
        f"- Did it try to call `litehive report`? What exact error did it hit?\n"
        f"- What does session metadata say about exit code and interruption state?\n"
        f"- What Litehive code path caused the failure?\n\n"
        f"Instructions:\n"
        f"- Read the traceback plus stdout/stderr/session/transcript artifacts and identify the Litehive bug\n"
        f"- Fix the bug with the smallest safe change in Litehive source code\n"
        f"- Run targeted verification for the Litehive fix; do not redo the failed stage's task work\n"
        f"- Submit your own detailed recovery report describing the root cause and Litehive fix\n"
        f"- When finished, submit your own detailed recovery report with `litehive report`\n"
        f"- Use `pass` only if the Litehive fix is in place and `{step}` should be retried\n"
        f"- Use `blocked` or `fail` if you cannot fix the Litehive bug\n"
    )


def _successful_thread_recovery_report(
    root: Path,
    task: TaskRecord,
    *,
    step: str,
    fallback_summary: str,
    failure_classification: str,
) -> StageReport | None:
    thread = load_task_thread(root, task)
    recovery_comments = [c for c in thread if c.step == step and c.verdict in ("pass", "accept")]
    if not recovery_comments:
        return None
    latest = recovery_comments[-1]
    summary = latest.message.splitlines()[0] if latest.message else fallback_summary
    return StageReport(
        task_id=task.id,
        step=step,
        verdict=latest.verdict,
        summary=summary,
        feedback=latest.message,
        files_changed=latest.files_changed,
        retry_decision="retry" if latest.verdict in ("pass", "accept") else "continue",
        failure_classification=failure_classification,
    )


def _record_runnable_recovery(
    root: Path,
    task: TaskRecord,
    *,
    trigger: str,
    step: str,
    summary: str,
    action: str,
    action_summary: str,
    failure_classification: str,
    warnings: list[str],
    recovery_subagent_id: str | None,
    recovery_subagent_path: str | None,
    metadata: dict[str, str] | None = None,
) -> None:
    action_payload = {
        "action": action,
        "summary": action_summary,
    }
    if metadata is not None:
        action_payload["metadata"] = metadata
    record_recovery_report(
        root,
        task,
        trigger=trigger,
        stage=step,
        summary=summary,
        runnable_state="runnable",
        failure_classification=failure_classification,
        actions=[
            RecoveryAction(**action_payload)
        ],
        warnings=warnings,
        recovery_subagent_id=recovery_subagent_id,
        recovery_subagent_path=recovery_subagent_path,
    )


def _record_blocked_recovery(
    root: Path,
    task: TaskRecord,
    *,
    trigger: str,
    step: str,
    summary: str,
    blocker: str,
    action: str,
    action_summary: str,
    failure_classification: str,
    warnings: list[str],
    recovery_subagent_id: str | None,
    recovery_subagent_path: str | None,
    metadata: dict[str, str] | None = None,
) -> None:
    action_payload = {
        "action": action,
        "applied": False,
        "summary": action_summary,
    }
    if metadata is not None:
        action_payload["metadata"] = metadata
    record_recovery_report(
        root,
        task,
        trigger=trigger,
        stage=step,
        summary=summary,
        runnable_state="blocked",
        failure_classification=failure_classification,
        blocker=blocker,
        actions=[
            RecoveryAction(**action_payload)
        ],
        warnings=warnings,
        recovery_subagent_id=recovery_subagent_id,
        recovery_subagent_path=recovery_subagent_path,
    )


def _subagent_ref_details(recovery_result) -> tuple[str | None, str | None]:
    if recovery_result is None or recovery_result.ref is None:
        return None, None
    return recovery_result.ref.id, recovery_result.ref.path


def _finalize_successful_recovery(
    root: Path,
    task: TaskRecord,
    *,
    step: str,
    trigger: str,
    failure_classification: str,
    failed_report: StageReport,
    recovery_result,
    action: str,
    action_summary: str,
    metadata: dict[str, str] | None = None,
) -> StageReport | None:
    recovery_report = _successful_thread_recovery_report(
        root,
        task,
        step=step,
        fallback_summary=f"{step} recovered",
        failure_classification=failure_classification,
    )
    if recovery_report is None:
        recovery_report = stage_report_from_subagent(task, step, recovery_result, root=root)
        if recovery_report.verdict in ("pass", "accept"):
            recovery_report.retry_decision = "retry"
            recovery_report.failure_classification = failure_classification
    if recovery_report.verdict not in ("pass", "accept"):
        return None
    subagent_id, subagent_path = _subagent_ref_details(recovery_result)
    _record_runnable_recovery(
        root,
        task,
        trigger=trigger,
        step=step,
        summary=recovery_report.summary,
        action=action,
        action_summary=action_summary,
        failure_classification=failure_classification,
        warnings=[*failed_report.warnings, *recovery_report.warnings],
        recovery_subagent_id=subagent_id,
        recovery_subagent_path=subagent_path,
        metadata=metadata,
    )
    append_journal(root, task, f"Recovery agent resolved {step}: {recovery_report.verdict}")
    return recovery_report


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
    del execution_root, role_name
    if subagents is None or _recovery_attempt_exhausted(root, task, step):
        return None
    evidence_lines = _recovery_evidence_lines(root, task, stage=step)
    diagnostics_text, report_attempt_summary = _load_failed_subagent_diagnostics(root, task)
    failure_owner, traceback_text, source_root = _classify_recovery_failure_owner(
        root, failed_report, config=config
    )
    recovery_root = _resolve_recovery_execution_root(root, source_root)
    if recovery_root is None:
        _record_missing_litehive_source_repo(
            root,
            task,
            step=step,
            failed_report=failed_report,
            failure_owner=failure_owner,
            report_attempt_summary=report_attempt_summary,
        )
        return None
    is_self_heal = failure_owner == "litehive" and recovery_root == source_root
    if is_self_heal:
        fingerprint = _traceback_fingerprint(traceback_text, failed_report.summary)
        return _run_litehive_self_heal(
            root=root,
            source_root=recovery_root,
            task=task,
            step=step,
            failed_report=failed_report,
            traceback_text=traceback_text,
            fingerprint=fingerprint,
            evidence_lines=evidence_lines,
            diagnostics_text=diagnostics_text,
            engine_name=engine_name,
            model_name=model_name,
        )
    append_journal(
        root,
        task,
        f"Stage `{step}` {failed_report.verdict}: {failed_report.summary}. Launching recovery agent.",
    )
    recovery_result = SubagentManager(root, execution_root=recovery_root).run(
        task,
        role="recovery",
        engine_name=engine_name,
        prompt=_build_standard_recovery_prompt(
            task,
            step=step,
            failed_report=failed_report,
            failure_owner=failure_owner,
            recovery_root=recovery_root,
            evidence_lines=evidence_lines,
            diagnostics_text=diagnostics_text,
        ),
        model=model_name,
    )
    recovery_report = _finalize_successful_recovery(
        root,
        task,
        step=step,
        trigger="stage_failure",
        failure_classification=failure_owner,
        failed_report=failed_report,
        recovery_result=recovery_result,
        action="resume_current_stage",
        action_summary=f"Recovery agent repaired the task and returned `{step}` to a runnable state.",
    )
    if recovery_report is not None:
        return recovery_report
    failed_recovery = stage_report_from_subagent(task, step, recovery_result, root=root)
    subagent_id, subagent_path = _subagent_ref_details(recovery_result)
    _record_blocked_recovery(
        root,
        task,
        trigger="stage_failure",
        step=step,
        summary=f"Recovery agent could not restore `{step}` to a runnable state.",
        blocker=failed_recovery.summary or failed_report.summary,
        action="no_safe_repair",
        action_summary="Recovery agent investigated the failure but could not apply a safe bounded repair.",
        failure_classification=failed_report.failure_classification or failed_report.verdict,
        warnings=[*failed_report.warnings, *failed_recovery.warnings],
        recovery_subagent_id=subagent_id,
        recovery_subagent_path=subagent_path,
    )
    append_journal(root, task, f"Recovery agent could not resolve {step}.")
    return None


def _run_litehive_self_heal(
    root: Path,
    source_root: Path,
    task: TaskRecord,
    step: str,
    failed_report: StageReport,
    traceback_text: str,
    fingerprint: str,
    evidence_lines: str,
    diagnostics_text: str,
    *,
    engine_name: str,
    model_name: str | None,
) -> StageReport | None:
    if fingerprint in task.runtime.self_heal_traceback_fingerprints:
        append_journal(
            root,
            task,
            f"Stage `{step}` {failed_report.verdict}: skipping duplicate self-heal (fingerprint {fingerprint}).",
        )
        return None
    append_journal(
        root,
        task,
        f"Stage `{step}` {failed_report.verdict}: litehive-owned failure. Launching self-heal agent against {source_root} (fingerprint {fingerprint}).",
    )
    recovery_result = SubagentManager(root, execution_root=source_root).run(
        task,
        role="recovery",
        engine_name=engine_name,
        prompt=_build_self_heal_prompt(
            task,
            step=step,
            failed_report=failed_report,
            traceback_text=traceback_text,
            source_root=source_root,
            evidence_lines=evidence_lines,
            diagnostics_text=diagnostics_text,
        ),
        model=model_name,
    )
    task.runtime.self_heal_traceback_fingerprints.append(fingerprint)
    save_task_runtime(root, task)
    recovery_report = _finalize_successful_recovery(
        root,
        task,
        step=step,
        trigger="litehive_self_heal",
        failure_classification="litehive",
        failed_report=failed_report,
        recovery_result=recovery_result,
        action="litehive_self_heal",
        action_summary=f"Self-heal agent fixed litehive source and returned `{step}` to a runnable state.",
        metadata={"fingerprint": fingerprint},
    )
    if recovery_report is not None:
        return recovery_report
    failed_recovery = stage_report_from_subagent(task, step, recovery_result, root=root)
    subagent_id, subagent_path = _subagent_ref_details(recovery_result)
    _record_blocked_recovery(
        root,
        task,
        trigger="litehive_self_heal",
        step=step,
        summary=f"Self-heal agent could not fix litehive bug for `{step}`.",
        blocker=failed_recovery.summary or failed_report.summary,
        action="litehive_self_heal_failed",
        action_summary="Self-heal agent could not fix the litehive bug.",
        failure_classification="litehive",
        warnings=[*failed_report.warnings, *failed_recovery.warnings],
        recovery_subagent_id=subagent_id,
        recovery_subagent_path=subagent_path,
        metadata={"fingerprint": fingerprint},
    )
    append_journal(root, task, f"Self-heal agent could not fix litehive bug for {step}.")
    return None


def _resolve_recovery_engine(
    root: Path,
    task: TaskRecord,
    config: LitehiveConfig | None,
) -> tuple[str, str | None]:
    from .._models import resolve_model, select_engine

    model: str | None = None
    if config and config.recovery_engine and config.recovery_engine != "auto":
        engine = config.recovery_engine
    elif config and config.recovery_engine == "auto":
        candidates = list(config.engine_preference) if config.engine_preference else []
        if config.default_engine and config.default_engine not in candidates:
            candidates.append(config.default_engine)
        if not candidates:
            candidates = ["claude", "codex", "copilot", "goz"]
        selection = select_engine(
            root,
            task,
            config,
            engine_names=candidates,
            require_available=True,
        )
        engine = selection.engine_name or config.default_engine or "codex"
        model = selection.model_name if selection.engine_name is not None else None
    else:
        engine = config.default_engine if config else "codex"
    if model is None:
        model = resolve_model(task, config, engine_name=engine) if config else None
    return engine, model


def _attempt_commit_recovery(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    reason: str,
    *,
    subagents: SubagentManager | None = None,
    config: LitehiveConfig | None = None,
) -> str | None:
    del execution_root
    if subagents is None:
        return None
    engine_name, model = _resolve_recovery_engine(root, task, config)
    recovery_result = subagents.run(
        task,
        role="recovery",
        engine_name=engine_name,
        model=model,
        prompt=f"CommitToGit failed: {reason}\nInvestigate and fix the commit failure, then complete the commit.",
    )
    subagent_id, subagent_path = _subagent_ref_details(recovery_result)
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
    return {path: path.read_text(encoding="utf-8") if path.exists() else None for path in paths}


def _restore_persisted_files(snapshot: dict[Path, str | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            if path.exists():
                path.unlink()
            continue
        _atomic_write_text(path, content)


def rollback_completed_task(root: Path, task_id: str) -> RollbackSummary:
    from .._types import RollbackSummary

    root = root.resolve()
    with workspace_mutation_guard(root), _workspace_lock(root):
        from litehive.tasks.crud import get_task

        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        _require_completed_task(task, action="rollback")
        attempt = task.git.checkpoint_attempts
        recovery_stage = implementation_entry_stage(task)
        state = load_state(root)
        snapshot = _capture_persisted_files(
            [task_file(root, task), task_runtime_file(root, task), state_path(root), task_dir(root, task) / "journal.md"]
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
                journal_message="Checkpoint rollback requested.\n"
                f"- rolled_back_attempt: `{attempt}`\n"
                f"- recovery_stage: `{recovery_stage}`",
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
        from litehive.tasks.crud import get_task

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
