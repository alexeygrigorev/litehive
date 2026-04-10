import os

from litehive.config import resolve_workspace
from litehive.models import TaskThreadComment
from litehive.tasks.crud import get_task
from litehive.tasks.persistence import load_state
from litehive.tasks.reports import append_thread_comment


def _cmd_report(args):
    task_id = args.task_id
    if not task_id:
        task_id = os.environ.get("LITEHIVE_TASK_ID")
    try:
        root = resolve_workspace(task_id, workspace=args.workspace)
    except ValueError as exc:
        print(f"report failed: {exc}")
        return 1
    if not task_id:
        state = load_state(root)
        task_id = state.active_task_id
    if not task_id:
        print("report failed: no task id provided, LITEHIVE_TASK_ID is unset, and no active task exists")
        return 1
    task = get_task(root, task_id)
    if task is None:
        print(f"report failed: task {task_id} not found")
        return 1
    step = args.step or task.pipeline_status
    normalized_verdict = "reject" if args.verdict == "fail" else args.verdict
    comment = TaskThreadComment(
        role=args.role,
        step=step,
        verdict=normalized_verdict,
        message=args.message,
        files_changed=list(getattr(args, 'files_changed', []) or []),
    )
    append_thread_comment(root, task, comment)
    print(f"task: {task.id}")
    print(f"step: {step}")
    print(f"verdict: {comment.verdict}")
    print(f"role: {args.role}")
    return 0
