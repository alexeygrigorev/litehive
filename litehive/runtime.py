"""High-level runtime flow for executing queued tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from litehive.config import load_config, load_context
from litehive.git_ops import GitError, commit_task
from litehive.models import StageReport, TaskRecord
from litehive.runner import RunResult, TaskExecutionRunner
from litehive.subagents import SubagentManager, stage_prompt, stage_report_from_subagent
from litehive.tasks import append_journal, clear_active_task, dequeue_next_task, get_task, load_state


@dataclass(slots=True)
class ExecutionSummary:
    task: TaskRecord | None
    result: RunResult | None
    commit_sha: str | None = None


def resolve_next_task(root: Path) -> TaskRecord | None:
    root = root.resolve()
    state = load_state(root)
    if state.active_task_id:
        return get_task(root, state.active_task_id)
    if not state.queue:
        return None
    return get_task(root, state.queue[0])


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

    def executor(current_task: TaskRecord, step: str) -> StageReport:
        prompt = stage_prompt(current_task, step, workspace_context=workspace_context)
        result = subagents.run(
            current_task,
            role=_role_for_step(step),
            engine_name=engine_name,
            prompt=prompt,
            model=config.opencode_model if engine_name == "opencode" else None,
        )
        return stage_report_from_subagent(current_task, step, result)

    runner = TaskExecutionRunner(root, executor)
    result = runner.run(task)
    append_journal(root, task, f"Execution finished with status `{result.final_status}`.")

    commit_sha = None
    if result.final_status == "done" and config.auto_commit and task.git.auto_commit:
        try:
            commit_sha = commit_task(root, task)
        except GitError as exc:
            append_journal(root, task, f"Auto-commit failed: {exc}")

    clear_active_task(root)
    return ExecutionSummary(task=task, result=result, commit_sha=commit_sha)


def _role_for_step(step: str) -> str:
    return {
        "grooming": "pm",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "pm",
    }.get(step, "swe")
