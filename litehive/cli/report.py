import os
from pathlib import Path

from litehive.models import TaskThreadComment
from litehive.tasks.crud import get_task
from litehive.tasks.persistence import load_state
from litehive.tasks.reports import append_thread_comment


def _resolve_workspace_root(workspace: Path) -> Path:
    """Resolve back to the main workspace root if running inside a worktree."""
    parts = workspace.resolve().parts
    for i, part in enumerate(parts):
        if part == ".litehive" and i + 2 < len(parts) and parts[i + 1] == "worktrees":
            return Path(*parts[:i])
    return workspace


def _cmd_report(args):
    root = _resolve_workspace_root(args.workspace)
    task_id = args.task_id
    if not task_id:
        task_id = os.environ.get("LITEHIVE_TASK_ID")
    if not task_id:
        state = load_state(root)
        task_id = state.active_task_id
    if not task_id:
        print("report failed: no active task and --task-id not provided")
        return 1
    task = get_task(root, task_id)
    if task is None:
        print(f"report failed: task {task_id} not found")
        return 1
    step = args.step or task.pipeline_status
    comment = TaskThreadComment(
        role=args.role,
        step=step,
        verdict=args.verdict,
        message=args.message,
    )
    append_thread_comment(root, task, comment)
    print(f"task: {task.id}")
    print(f"step: {step}")
    print(f"verdict: {args.verdict}")
    print(f"role: {args.role}")
    return 0
