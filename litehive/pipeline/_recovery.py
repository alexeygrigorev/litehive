"""Recovery agent logic: stage recovery, commit recovery, rollback, and recover."""

import hashlib
import re
from pathlib import Path

import yaml

from litehive.config import LitehiveConfig
from litehive.git import (
    GitError,
    abort_revert,
    commit_task,
    has_changes,
    rollback_message,
    rollback_task,
    current_head,
)
from litehive.models import RecoveryAction, StageReport, TaskRecord
from litehive.agents import SubagentManager, stage_report_from_subagent
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
    save_task_runtime,
    state_path,
    task_dir,
    task_file,
    task_runtime_file,
    workspace_mutation_guard,
)
from litehive.tasks.paths import _latest_subagent_base, _read_text_artifact, _resolve_artifact_path

from ._models import resolve_model
from ._types import RollbackSummary, _path_within

_RECOVERY_ARTIFACT_TEXT_LIMIT = 4000
_RECOVERY_REPORT_ATTEMPT_LINE_LIMIT = 8


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


def _truncate_recovery_text(text: str, *, limit: int = _RECOVERY_ARTIFACT_TEXT_LIMIT) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit].rstrip()}\n...[truncated]..."


def _load_failed_subagent_diagnostics(root: Path, task: TaskRecord) -> tuple[str, str]:
    base = _latest_subagent_base(root, task)
    if base is None:
        missing = "No failed subagent artifact directory was found for this task."
        return missing, missing

    session_path = _resolve_artifact_path(base, "session.yaml")
    stdout_path = _resolve_artifact_path(base, "stdout.txt")
    stderr_path = _resolve_artifact_path(base, "stderr.txt")
    transcript_path = _resolve_artifact_path(base, "transcript.md")
    prompt_path = _resolve_artifact_path(base, "prompt.txt")

    session_text = _read_text_artifact(session_path) if session_path is not None else ""
    stdout_text = _read_text_artifact(stdout_path) if stdout_path is not None else ""
    stderr_text = _read_text_artifact(stderr_path) if stderr_path is not None else ""
    transcript_text = _read_text_artifact(transcript_path) if transcript_path is not None else ""
    prompt_text = _read_text_artifact(prompt_path) if prompt_path is not None else ""

    exit_code: str | int | None = None
    if session_text:
        try:
            session_payload = yaml.safe_load(session_text) or {}
        except yaml.YAMLError:
            session_payload = {}
        if isinstance(session_payload, dict):
            exit_code = session_payload.get("exit_code")

    report_attempt_lines: list[str] = []
    for label, text in (
        ("prompt", prompt_text),
        ("stdout", stdout_text),
        ("stderr", stderr_text),
        ("transcript", transcript_text),
        ("session", session_text),
    ):
        for raw_line in text.splitlines():
            if "litehive report" not in raw_line:
                continue
            report_attempt_lines.append(f"{label}: {raw_line.strip()}")
            if len(report_attempt_lines) >= _RECOVERY_REPORT_ATTEMPT_LINE_LIMIT:
                break
        if len(report_attempt_lines) >= _RECOVERY_REPORT_ATTEMPT_LINE_LIMIT:
            break

    report_attempt_summary = (
        "No explicit `litehive report` command was found in the captured artifacts."
        if not report_attempt_lines
        else "Found `litehive report` clues:\n" + "\n".join(f"- {line}" for line in report_attempt_lines)
    )

    diagnostic_sections = [
        f"Failed subagent artifact base: {base.relative_to(root)}",
        f"Failed subagent exit code: {exit_code if exit_code is not None else 'unknown'}",
        report_attempt_summary,
        "",
        "Failed subagent session.yaml:",
        _truncate_recovery_text(session_text or "(missing)"),
        "",
        "Failed subagent stdout:",
        _truncate_recovery_text(stdout_text or "(missing)"),
        "",
        "Failed subagent stderr:",
        _truncate_recovery_text(stderr_text or "(missing)"),
        "",
        "Failed subagent transcript:",
        _truncate_recovery_text(transcript_text or "(missing)"),
    ]
    return "\n".join(diagnostic_sections), report_attempt_summary


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


def _resolve_recovery_execution_root(
    root: Path,
    source_root: Path | None,
) -> Path | None:
    if source_root is not None and source_root.is_dir():
        return source_root
    if source_root is None:
        return root
    if (root / "litehive").is_dir():
        return root
    return None


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

    # Only one recovery attempt per task. If it doesn't fix it, flag the task.
    MAX_RECOVERY_ATTEMPTS = 1
    recovery_count = sum(1 for sa in task.subagents if sa.role == "recovery")
    if recovery_count >= MAX_RECOVERY_ATTEMPTS:
        append_journal(
            root, task,
            f"[recovery] Skipping recovery for `{step}`: {recovery_count} recovery attempts exhausted (limit: {MAX_RECOVERY_ATTEMPTS}).",
        )
        return None

    evidence = collect_recovery_evidence(root, task, stage=step)
    evidence_lines = "\n".join(
        f"- {item.label}: {item.path or 'n/a'} ({'present' if item.exists else 'missing'}) :: {item.summary}"
        for item in evidence
    )
    diagnostics_text, report_attempt_summary = _load_failed_subagent_diagnostics(root, task)
    failure_owner, traceback_text, source_root = _classify_recovery_failure_owner(
        root,
        failed_report,
        config=config,
    )
    recovery_root = _resolve_recovery_execution_root(root, source_root)

    if recovery_root is None:
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
        return None

    # --- Litehive self-heal path ---
    is_self_heal = failure_owner == "litehive" and recovery_root == source_root
    if is_self_heal:
        fingerprint = _traceback_fingerprint(traceback_text, failed_report.summary)
        if fingerprint in task.runtime.self_heal_traceback_fingerprints:
            append_journal(
                root, task,
                f"Stage `{step}` {failed_report.verdict}: skipping duplicate self-heal (fingerprint {fingerprint}).",
            )
            return None
        self_heal_subagents = SubagentManager(root, execution_root=recovery_root)
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
            subagents=self_heal_subagents,
            engine_name=engine_name,
            model_name=model_name,
        )

    # --- Standard project recovery path ---
    append_journal(
        root, task,
        f"Stage `{step}` {failed_report.verdict}: {failed_report.summary}. Launching recovery agent.",
    )
    recovery_subagents = SubagentManager(root, execution_root=recovery_root)
    prompt = (
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
        + f"\n\nWhen you finish, submit your own detailed recovery report with `litehive report`.\n"
        f"Use verdict `pass` only if you fixed a Litehive issue and `{step}` should now be retried with the fix in place.\n"
        f"Use verdict `blocked` or `fail` if a blocker remains or no Litehive fix was possible.\n"
    )

    recovery_result = recovery_subagents.run(
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
            retry_decision="retry" if latest.verdict in ("pass", "accept") else "continue",
            failure_classification=failure_owner,
        )

    recovery_report = stage_report_from_subagent(task, step, recovery_result, root=root)
    if recovery_report.verdict in ("pass", "accept"):
        recovery_report.retry_decision = "retry"
        recovery_report.failure_classification = failure_owner
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
    subagents: SubagentManager,
    engine_name: str,
    model_name: str | None,
) -> StageReport | None:
    """Launch a recovery agent against litehive source to fix a litehive bug."""
    append_journal(
        root, task,
        f"Stage `{step}` {failed_report.verdict}: litehive-owned failure. "
        f"Launching self-heal agent against {source_root} (fingerprint {fingerprint}).",
    )

    prompt = (
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

    recovery_result = subagents.run(
        task,
        role="recovery",
        engine_name=engine_name,
        prompt=prompt,
        model=model_name,
    )

    # Record fingerprint regardless of outcome to prevent retry loops
    task.runtime.self_heal_traceback_fingerprints.append(fingerprint)
    save_task_runtime(root, task)

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
            trigger="litehive_self_heal",
            stage=step,
            summary=latest.message.splitlines()[0] if latest.message else f"self-heal recovered {step}",
            runnable_state="runnable",
            failure_classification="litehive",
            actions=[
                RecoveryAction(
                    action="litehive_self_heal",
                    summary=f"Self-heal agent fixed litehive source and returned `{step}` to a runnable state.",
                    metadata={"verdict": latest.verdict, "fingerprint": fingerprint},
                )
            ],
            warnings=list(failed_report.warnings),
            recovery_subagent_id=recovery_result.ref.id,
            recovery_subagent_path=recovery_result.ref.path,
        )
        append_journal(root, task, f"Self-heal agent resolved {step}: {latest.verdict}")
        return StageReport(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            verdict=latest.verdict,  # type: ignore[arg-type]
            summary=latest.message.splitlines()[0] if latest.message else f"self-heal recovered {step}",
            feedback=latest.message,
            files_changed=latest.files_changed,
            retry_decision="retry" if latest.verdict in ("pass", "accept") else "continue",
            failure_classification="litehive",
        )

    recovery_report = stage_report_from_subagent(task, step, recovery_result, root=root)
    if recovery_report.verdict in ("pass", "accept"):
        recovery_report.retry_decision = "retry"
        recovery_report.failure_classification = "litehive"
        record_recovery_report(
            root,
            task,
            trigger="litehive_self_heal",
            stage=step,
            summary=recovery_report.summary,
            runnable_state="runnable",
            failure_classification="litehive",
            actions=[
                RecoveryAction(
                    action="litehive_self_heal",
                    summary=f"Self-heal agent fixed litehive source and returned `{step}` to a runnable state.",
                    metadata={"verdict": recovery_report.verdict, "fingerprint": fingerprint},
                )
            ],
            warnings=[*failed_report.warnings, *recovery_report.warnings],
            recovery_subagent_id=recovery_result.ref.id,
            recovery_subagent_path=recovery_result.ref.path,
        )
        append_journal(root, task, f"Self-heal agent resolved {step}: {recovery_report.verdict}")
        return recovery_report

    blocker = recovery_report.summary or failed_report.summary
    record_recovery_report(
        root,
        task,
        trigger="litehive_self_heal",
        stage=step,
        summary=f"Self-heal agent could not fix litehive bug for `{step}`.",
        runnable_state="blocked",
        failure_classification="litehive",
        blocker=blocker,
        actions=[
            RecoveryAction(
                action="litehive_self_heal_failed",
                applied=False,
                summary="Self-heal agent could not fix the litehive bug.",
                metadata={"fingerprint": fingerprint},
            )
        ],
        warnings=[*failed_report.warnings, *recovery_report.warnings],
        recovery_subagent_id=recovery_result.ref.id,
        recovery_subagent_path=recovery_result.ref.path,
    )
    append_journal(root, task, f"Self-heal agent could not fix litehive bug for {step}.")
    return None


def _resolve_recovery_engine(
    task: TaskRecord,
    config: "LitehiveConfig | None",
) -> tuple[str, str | None]:
    if config and config.recovery_engine and config.recovery_engine != "auto":
        engine = config.recovery_engine
    elif config and config.recovery_engine == "auto":
        # Pick the first available engine from preference list or fallback to default
        from litehive.agents import get_engine
        candidates = list(config.engine_preference) if config.engine_preference else []
        if config.default_engine and config.default_engine not in candidates:
            candidates.append(config.default_engine)
        if not candidates:
            candidates = ["claude", "codex", "copilot", "goz"]
        engine = config.default_engine or "codex"
        for name in candidates:
            try:
                if get_engine(name).is_available():
                    engine = name
                    break
            except Exception:
                continue
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
