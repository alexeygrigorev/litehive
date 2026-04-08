import os

from litehive.models import TaskThreadComment
from litehive.tasks import append_thread_comment, load_state, get_task


def _cmd_report(args):
    root = args.workspace
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
