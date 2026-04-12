import os

from litehive.config import resolve_workspace
from litehive.models import TaskThreadComment
from litehive.tasks.crud import get_task
from litehive.tasks.persistence import load_state
from litehive.tasks.reports import append_thread_comment


def cmd_report(
    workspace,
    verdict,
    message,
    role="swe",
    step=None,
    task_id=None,
    files_changed=None,
):
    from litehive.cli.agent_cli import block_if_agent

    block_if_agent()
    if not task_id:
        task_id = os.environ.get("LITEHIVE_TASK_ID")
    try:
        root = resolve_workspace(task_id, workspace=workspace)
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
    step = step or task.pipeline_status
    normalized_verdict = "reject" if verdict == "fail" else verdict
    comment = TaskThreadComment(
        role=role,
        step=step,
        verdict=normalized_verdict,
        message=message,
        files_changed=list(files_changed or []),
    )
    append_thread_comment(root, task, comment)
    print(f"task: {task.id}")
    print(f"step: {step}")
    print(f"verdict: {comment.verdict}")
    print(f"role: {role}")
    return 0
