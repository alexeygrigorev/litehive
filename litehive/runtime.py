"""High-level runtime flow for executing queued tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

import yaml

from litehive.config import LitehiveConfig, load_config, load_context
from litehive.git_ops import (
    GitError,
    checkpoint_message,
    commit_task,
    current_head,
    is_git_repo,
    rollback_message,
    rollback_task,
    status_porcelain,
)
from litehive.models import StageReport, TaskRecord
from litehive.runner import RunResult, StageExecutor, TaskExecutionRunner
from litehive.subagents import SubagentManager, stage_prompt, stage_report_from_subagent
from litehive.tasks import (
    append_journal,
    enqueue_task,
    clear_active_task,
    dequeue_next_task,
    get_task,
    mark_engine_switch,
    mark_task_run_finished,
    mark_task_run_started,
    peek_next_task,
    save_task_runtime,
    save_task,
)


@dataclass(slots=True)
class ExecutionSummary:
    task: TaskRecord | None
    result: RunResult | None
    commit_sha: str | None = None


@dataclass(slots=True)
class TaskPoolRunSummary:
    executions: list[ExecutionSummary]
    stop_reason: str


@dataclass(slots=True)
class RollbackSummary:
    task: TaskRecord
    rollback_sha: str
    rolled_back_sha: str


def resolve_next_task(root: Path) -> TaskRecord | None:
    root = root.resolve()
    return peek_next_task(root)


def run_next_task(root: Path) -> ExecutionSummary:
    root = root.resolve()
    task = dequeue_next_task(root)
    if task is None:
        return ExecutionSummary(task=None, result=None)

    config = load_config(root)
    workspace_context = load_context(root)
    engine_name = task.engine or config.default_engine
    subagents = SubagentManager(root)

    append_journal(root, task, f"Execution started with engine `{engine_name}`.")
    mark_task_run_started(root, task)

    runner = TaskExecutionRunner(
        root,
        build_executor(
            root,
            initial_engine_name=engine_name,
            workspace_context=workspace_context,
            subagents=subagents,
            config=config,
            config_auto_commit=config.auto_commit,
        ),
    )
    result = runner.run(task)
    mark_task_run_finished(root, task, result.final_status)
    if result.final_status != "done":
        append_journal(root, task, f"Execution finished with status `{result.final_status}`.")
    clear_active_task(root)

    return ExecutionSummary(task=task, result=result, commit_sha=task.git.commit_sha)


def run_task_pool(
    root: Path,
    *,
    stop_when: Callable[[list[ExecutionSummary]], bool] | None = None,
) -> TaskPoolRunSummary:
    root = root.resolve()
    executions: list[ExecutionSummary] = []

    while True:
        if stop_when is not None and stop_when(executions):
            return TaskPoolRunSummary(executions=executions, stop_reason="stop_condition_reached")
        execution = run_next_task(root)
        if execution.task is None:
            return TaskPoolRunSummary(executions=executions, stop_reason="queue_exhausted")
        executions.append(execution)


def rollback_completed_task(root: Path, task_id: str) -> RollbackSummary:
    root = root.resolve()
    task = get_task(root, task_id)
    if task is None:
        raise GitError(f"Task {task_id} not found")
    _require_completed_task(task, action="rollback")

    rollback = rollback_task(root, task)
    attempt = task.git.checkpoint_attempts
    task.status = "queued"
    task.pipeline_status = "implementing"
    task.git.commit_sha = None
    task.git.rolled_back_checkpoint_attempt = attempt
    append_journal(
        root,
        task,
        (
            "Checkpoint rollback requested.\n"
            f"- rolled_back: `{rollback.rolled_back_sha}`\n"
            f"- recovery_stage: `implementing`"
        ),
    )
    save_task(root, task)

    rollback_checkpoint = commit_task(root, rollback_message(task, attempt))
    if rollback_checkpoint is None:
        raise GitError("git rollback commit failed")

    enqueue_task(root, task.id)
    return RollbackSummary(
        task=task,
        rollback_sha=rollback_checkpoint.commit_sha,
        rolled_back_sha=rollback.rolled_back_sha,
    )


def recover_completed_task(root: Path, task_id: str) -> TaskRecord:
    root = root.resolve()
    task = get_task(root, task_id)
    if task is None:
        raise GitError(f"Task {task_id} not found")
    _require_completed_task(task, action="recover")

    task.status = "queued"
    task.pipeline_status = "implementing"
    task.git.commit_sha = None
    append_journal(root, task, "Task recovered for another implementation pass.")
    save_task(root, task)
    enqueue_task(root, task.id)
    return task


def _role_for_step(step: str) -> str:
    return {
        "grooming": "pm",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "pm",
    }.get(step, "swe")


def _require_completed_task(task: TaskRecord, action: str) -> None:
    if task.status != "done" or task.pipeline_status != "done":
        raise GitError(f"Task {task.id} is not completed; cannot {action}")


def build_executor(
    root: Path,
    *,
    initial_engine_name: str,
    workspace_context: str,
    subagents: SubagentManager,
    config: LitehiveConfig,
    config_auto_commit: bool,
) -> StageExecutor:
    def executor(current_task: TaskRecord, step: str) -> StageReport:
        if step == "commit_to_git":
            return _commit_to_git_report(
                root,
                current_task,
                auto_commit_enabled=config_auto_commit and current_task.git.auto_commit,
            )

        prompt = stage_prompt(current_task, step, workspace_context=workspace_context)
        fallback_events: list[str] = []
        engines = _engine_attempt_order(initial_engine_name, config.engine_fallbacks)

        for index, engine_name in enumerate(engines):
            result = subagents.run(
                current_task,
                role=_role_for_step(step),
                engine_name=engine_name,
                prompt=prompt,
                model=_model_for_engine(config, engine_name),
            )
            is_limit_failure = result.failure is not None and result.failure.kind == "execution_limit"
            if is_limit_failure and index + 1 < len(engines):
                next_engine = engines[index + 1]
                event = (
                    f"Stage `{step}` switched from `{engine_name}` to `{next_engine}` "
                    f"after {result.failure.reason}."
                )
                fallback_events.append(event)
                append_journal(root, current_task, event)
                mark_engine_switch(
                    root,
                    current_task,
                    step=step,
                    from_engine=engine_name,
                    to_engine=next_engine,
                    reason=result.failure.reason,
                )
                continue

            report = stage_report_from_subagent(current_task, step, result)
            if fallback_events:
                report.warnings = [*fallback_events, *report.warnings]
            if is_limit_failure:
                report.summary = (
                    f"{step} blocked after exhausting engine fallbacks: {result.failure.reason}"
                )
                report.feedback = "\n\n".join([*fallback_events, result.transcript]).strip()
                if not report.warnings or report.warnings[-1] != result.failure.reason:
                    report.warnings.append(result.failure.reason)
            return report

        raise RuntimeError("Engine attempt order must include at least one engine")

    return executor


def _model_for_engine(config: LitehiveConfig, engine_name: str) -> str | None:
    if engine_name == "opencode":
        return config.opencode_model
    if engine_name == "gemini":
        return config.gemini_model
    return None


def _engine_attempt_order(initial_engine_name: str, engine_fallbacks: dict[str, list[str]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for engine_name in [initial_engine_name, *engine_fallbacks.get(initial_engine_name, [])]:
        if engine_name in seen:
            continue
        seen.add(engine_name)
        ordered.append(engine_name)
    return ordered


def _commit_to_git_report(root: Path, task: TaskRecord, *, auto_commit_enabled: bool) -> StageReport:
    if not auto_commit_enabled:
        task.status = "done"
        task.pipeline_status = "done"
        save_task(root, task)
        append_journal(root, task, "CommitToGit skipped: auto-commit disabled.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="pass",
            summary="CommitToGit skipped because auto-commit is disabled",
            warnings=["auto-commit disabled"],
        )

    if not is_git_repo(root):
        append_journal(root, task, "CommitToGit failed: workspace is not a git repository.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: workspace is not a git repository",
            warnings=["workspace is not a git repository"],
        )

    try:
        dirty_entries = status_porcelain(root)
    except GitError as exc:
        append_journal(root, task, f"CommitToGit failed: {exc}")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary=f"CommitToGit failed: {exc}",
            warnings=[str(exc)],
        )

    if not dirty_entries:
        append_journal(root, task, "CommitToGit failed: repository has no changes to commit.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: repository has no changes to commit",
            warnings=["no changes to commit"],
        )

    unexpected_dirty_paths = _unexpected_dirty_paths(root, task, dirty_entries)
    if unexpected_dirty_paths:
        message = (
            "repository has unrelated changes: " + ", ".join(f"`{path}`" for path in unexpected_dirty_paths)
        )
        append_journal(root, task, f"CommitToGit failed: {message}.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary=f"CommitToGit failed: {message}",
            warnings=["repository contains unrelated uncommitted changes"],
        )

    try:
        base_sha = current_head(root)
        attempt = task.git.checkpoint_attempts + 1
        message = checkpoint_message(task, attempt=attempt)
        previous_base_sha = task.git.checkpoint_base_sha
        previous_attempts = task.git.checkpoint_attempts
        previous_rollback_attempt = task.git.rolled_back_checkpoint_attempt
        previous_status = task.status
        previous_pipeline_status = task.pipeline_status
        task.git.commit_sha = None
        task.git.checkpoint_base_sha = base_sha
        task.git.checkpoint_attempts = attempt
        task.git.rolled_back_checkpoint_attempt = None
        task.status = "done"
        task.pipeline_status = "done"
        append_journal(
            root,
            task,
            (
                "CommitToGit requested.\n"
                f"- base: `{base_sha or 'initial commit'}`\n"
                f"- message: `{message}`"
            ),
        )
        save_task(root, task)
        checkpoint = commit_task(root, message)
        if checkpoint is None:
            raise GitError("git commit prerequisites were not met")
    except GitError as exc:
        task.git.checkpoint_base_sha = previous_base_sha
        task.git.checkpoint_attempts = previous_attempts
        task.git.rolled_back_checkpoint_attempt = previous_rollback_attempt
        task.git.commit_sha = None
        task.status = previous_status
        task.pipeline_status = previous_pipeline_status
        save_task(root, task)
        append_journal(root, task, f"CommitToGit failed: {exc}")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary=f"CommitToGit failed: {exc}",
            warnings=[str(exc)],
        )

    task.git.commit_sha = checkpoint.commit_sha
    save_task_runtime(root, task)
    return StageReport(
        task_id=task.id,
        step="commit_to_git",
        verdict="pass",
        summary="CommitToGit created the final checkpoint commit",
    )


def _unexpected_dirty_paths(root: Path, task: TaskRecord, dirty_entries: list[str]) -> list[str]:
    unexpected: list[str] = []
    for entry in dirty_entries:
        path = _status_entry_path(entry)
        if path is None or _is_allowed_commit_path(root, task, path):
            continue
        unexpected.append(path)
    return unexpected


def _allowed_commit_paths(root: Path, task: TaskRecord) -> set[PurePosixPath]:
    allowed = {
        PurePosixPath(".litehive") / "config.yaml",
        PurePosixPath(".litehive") / "context.md",
        PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}",
    }
    reports_dir = root / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    for report_path in reports_dir.glob("*.yaml"):
        report_data = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        for changed in report_data.get("files_changed") or []:
            normalized = str(changed).strip()
            if normalized:
                allowed.add(PurePosixPath(normalized))
    return allowed


def _is_allowed_commit_path(root: Path, task: TaskRecord, path: str) -> bool:
    candidate = PurePosixPath(path)
    for allowed in _allowed_commit_paths(root, task):
        if candidate == allowed or allowed in candidate.parents:
            return True
    return False


def _status_entry_path(entry: str) -> str | None:
    if len(entry) < 4:
        return None
    path = entry[3:]
    if " -> " in path:
        return path.split(" -> ", 1)[1].strip()
    return path.strip() or None
