from pathlib import Path
from typing import Annotated

import typer

from heru import ENGINE_CHOICES
from litehive.cli.common import WorkspaceOption, choice, make_typer
from litehive.cli.display import (
    task_dependencies_label,
    task_engine_label,
    task_interruption_label,
    task_model_label,
)
from litehive.config.workspace import create_workspace
from litehive.container import build_workspace
from litehive.git.ops import GitError, checkpoint_message
from litehive.recovery.execution_recovery import recover_stale_runner_state_for_workspace
from litehive.tasks.completed_task_recovery import recover_completed_task_for_workspace
from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.tasks.normalization import missing_acceptance_criteria_reason
from litehive.state.persist import load_state_for_workspace
from litehive.tasks.queue import move_queued_task_for_workspace, prioritize_queued_tasks_for_workspace
from litehive.tasks.status import (
    requeue_task_for_workspace,
    resume_task_for_workspace,
    stop_current_task,
    switch_task_engine_for_workspace,
)

app = make_typer(invoke_without_command=True)


@app.callback(invoke_without_command=True)
def queue_group(ctx: typer.Context, workspace: WorkspaceOption = Path.cwd()) -> int | None:
    """
    Default ``litehive queue`` view: active task, queued tasks, resumable tasks.

    Runs :func:`recover_stale_runner_state` first so a crashed runner
    does not leave the queue in a wedged state for the operator. The
    output is line-oriented (one ``key: value`` per row) so it can
    feed shell pipelines as well as a human reader.
    """
    if ctx.invoked_subcommand is not None:
        return None
    workspace_obj = build_workspace(workspace)
    config = workspace_obj.load_config()
    recover_stale_runner_state_for_workspace(workspace_obj)
    state = load_state_for_workspace(workspace_obj)
    tasks = workspace_obj.list_tasks()
    print(f"active_task_id: {state.active_task_id}")
    if state.active_task_id is not None:
        active_task = workspace_obj.require_task(state.active_task_id)
        print(
            f"active: {active_task.id} [{active_task.status}/{active_task.pipeline_status}] "
            f"priority={active_task.priority} engine={task_engine_label(None, config.default_engine)} "
            f"model={task_model_label(active_task.model)} "
            f"title={active_task.title} depends_on={task_dependencies_label(active_task.id, active_task.depends_on)}"
            f"{task_interruption_label(active_task)}"
        )
    print(f"queue_length: {len(state.queue)}")
    for index, queued_task_id in enumerate(state.queue, start=1):
        task = workspace_obj.require_task(queued_task_id)
        print(
            f"{index}. {task.id} [{task.status}/{task.pipeline_status}] "
            f"priority={task.priority} engine={task_engine_label(None, config.default_engine)} "
            f"model={task_model_label(task.model)} "
            f"title={task.title} depends_on={task_dependencies_label(task.id, task.depends_on)}"
            f"{task_interruption_label(task)}"
        )
    resumable = [task for task in tasks if task.status in {TaskStatus.INTERRUPTED, TaskStatus.PARKED}]
    print(f"resumable_tasks: {len(resumable)}")
    for index, task in enumerate(resumable, start=1):
        print(
            f"resume {index}. {task.id} [{task.status}/{task.pipeline_status}] "
            f"priority={task.priority} engine={task_engine_label(None, config.default_engine)} "
            f"model={task_model_label(task.model)} "
            f"title={task.title} depends_on={task_dependencies_label(task.id, task.depends_on)}"
            f"{task_interruption_label(task)}"
        )
    return 0


@app.command("move", help="Move a queued task to a 1-based position")
def move(
    task_id: Annotated[str, typer.Argument(help="Queued task id")],
    position: Annotated[int, typer.Argument(help="Target queue position (1-based)")],
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    """
    Reorder a queued task to an explicit 1-based slot.

    Lets operators hand-prioritize without restarting the queue or
    bulk-clearing it; the position is interpreted against the
    current queue at the moment of the call so chained moves
    behave predictably.
    """
    create_workspace(workspace)
    workspace_obj = build_workspace(workspace)
    try:
        state = move_queued_task_for_workspace(workspace_obj, task_id, position)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"move failed: {exc}")
        return 1
    print(f"task_id: {task_id}")
    print(f"position: {state.queue.index(task_id) + 1}")
    return 0


@app.command("promote", help="Move a queued task to the front of the queue")
def promote(
    task_id: Annotated[str, typer.Argument(help="Queued task id")], workspace: WorkspaceOption = Path.cwd()
) -> int:
    """
    Jump a task to the front of the queue.

    For a queued task this is a plain move-to-position-1; for a
    paused/closed task (interrupted, parked, flagged, closed) it
    resumes the task with ``front=True`` so the next runner picks
    it up immediately. Without this branching the operator would
    have to call ``resume`` then ``move`` separately.
    """
    create_workspace(workspace)
    workspace_obj = build_workspace(workspace)
    try:
        task = workspace_obj.require_task(task_id)
        if task.status in {TaskStatus.INTERRUPTED, TaskStatus.PARKED, TaskStatus.FLAGGED, TaskStatus.CLOSED}:
            task = resume_task_for_workspace(workspace_obj, task_id, front=True)
            print(f"task: {task.id} {task.title}")
            print("status: queued")
            print(f"pipeline_stage: {task.pipeline_status}")
            missing_criteria_reason = missing_acceptance_criteria_reason(task)
            if missing_criteria_reason is not None:
                print(f"warning: {missing_criteria_reason}")
            print("position: 1")
            return 0
        move_queued_task_for_workspace(workspace_obj, task_id, 1)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"promote failed: {exc}")
        return 1
    print(f"task_id: {task_id}")
    print("position: 1")
    return 0


@app.command("requeue", help="Requeue a parked, flagged, or closed task from the implementation entry stage")
def requeue(
    task_id: Annotated[str, typer.Argument(help="Task id to requeue")],
    workspace: WorkspaceOption = Path.cwd(),
    front: Annotated[bool, typer.Option(help="Insert at the front of the queue")] = False,
    force: Annotated[bool, typer.Option(help="Force requeue after repeated flagging")] = False,
) -> int:
    """
    Send a parked/flagged/closed task back through the implementation entry stage.

    For already-done tasks delegates to :func:`recover` so committed
    code is not reverted; the operator's intent in that case is
    "run again from a clean slate", not "redo the implementation
    from scratch". ``--force`` is only consulted in the non-done
    branch (the persistence layer needs the override to clear
    flagging counters).
    """
    create_workspace(workspace)
    workspace_obj = build_workspace(workspace)
    task = workspace_obj.get_task_record(task_id)
    if task is not None and (task.pipeline_status == PipelineStatus.DONE or task.status == TaskStatus.DONE):
        return recover(task_id, workspace)
    try:
        task = requeue_task_for_workspace(workspace_obj, task_id, front=front, force=force)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"requeue failed: {exc}")
        return 1
    state = load_state_for_workspace(workspace_obj)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_stage: {task.pipeline_status}")
    print(f"pipeline_status: {task.pipeline_status}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


@app.command("resume", help="Resume an interrupted, parked, flagged, or closed task at its current stage")
def resume(
    task_id: Annotated[str, typer.Argument(help="Task id to resume")],
    workspace: WorkspaceOption = Path.cwd(),
    front: Annotated[bool, typer.Option(help="Insert at the front of the queue")] = False,
) -> int:
    """
    Re-enqueue a paused/closed task at its saved pipeline stage.

    Distinct from ``requeue`` in that the task picks up where it
    left off rather than restarting from grooming/implementation.
    Used after the operator clears whatever caused the
    interruption (engine quota, sandbox failure, manual park).
    """
    create_workspace(workspace)
    workspace_obj = build_workspace(workspace)
    try:
        task = resume_task_for_workspace(workspace_obj, task_id, front=front)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"resume failed: {exc}")
        return 1
    state = load_state_for_workspace(workspace_obj)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_stage: {task.pipeline_status}")
    print(f"pipeline_status: {task.pipeline_status}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


@app.command("stop", help="Stop the current active task cleanly")
def stop(workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Halt the active task cleanly.

    Signals the runner to exit at the next checkpoint and returns
    the workspace to an idle state. Reports whether a SIGTERM was
    actually delivered (``signal_sent``) so the operator can tell
    a "no runner to signal" cleanup apart from a real interruption.
    """
    create_workspace(workspace)
    workspace_obj = build_workspace(workspace)
    try:
        summary = stop_current_task(workspace_obj)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"stop failed: {exc}")
        return 1
    if summary.runner_pid is not None:
        runner_pid_label = summary.runner_pid
    else:
        runner_pid_label = "-"
    if summary.signal_sent:
        signal_sent_label = "yes"
    else:
        signal_sent_label = "no"
    print(f"task: {summary.task.id} {summary.task.title}")
    print(f"status: {summary.task.status}")
    print(f"pipeline_stage: {summary.task.pipeline_status}")
    print(f"runner_pid: {runner_pid_label}")
    print(f"signal_sent: {signal_sent_label}")
    return 0


def prioritize(task_ids: list[str], workspace: Path) -> int:
    """
    Bulk-move queued task ids to the front of the queue, preserving order.

    Shared core for the ``prioritize`` command and any caller that
    builds the id list itself (without going through Typer
    argument parsing). Order matters: the first id ends up at
    position 1, the second at 2, and so on, regardless of where
    they were queued before.
    """
    create_workspace(workspace)
    workspace_obj = build_workspace(workspace)
    try:
        state = prioritize_queued_tasks_for_workspace(workspace_obj, task_ids)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"prioritize failed: {exc}")
        return 1
    print(f"moved_tasks: {' '.join(task_ids)}")
    print(f"moved_count: {len(task_ids)}")
    print(f"front_of_queue: {' '.join(state.queue[: len(task_ids)])}")
    print(f"queue_length: {len(state.queue)}")
    return 0


@app.command("prioritize", help="Move queued tasks to the front in the provided order")
def prioritize_command(
    task_ids: Annotated[list[str], typer.Argument(help="Queued task ids to move to the front")],
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    """
    Typer wrapper around :func:`prioritize`.

    Kept thin so the underlying helper can be reused (tests, other
    commands) without binding to Typer's argument parsing. The
    helper has no Typer-specific behavior — keeping it free
    avoids dragging the framework into call sites that just want
    to reorder queue ids.
    """
    return prioritize(task_ids, workspace)


def recover(task_id: str, workspace: Path) -> int:
    """
    Requeue a task that already reached the done/committed state.

    Does not roll back any code: ``recover`` is for the case where
    the operator wants the same task to run again on top of its
    committed result (for instance, after fixing a downstream
    integration). Called by the ``recover`` command and by
    ``requeue`` when it detects a completed task — the latter
    keeps ``requeue`` operator-friendly without forcing them to
    pick the right command.
    """
    create_workspace(workspace)
    workspace_obj = build_workspace(workspace)
    try:
        task = recover_completed_task_for_workspace(workspace_obj, task_id)
    except (GitError, WorkspaceConflictError) as exc:
        print(f"recover failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_stage: {task.pipeline_status}")
    print("recovery_policy: recover requeued the task without reverting workspace code")
    print(f"next_commit_message: {checkpoint_message(task)}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


@app.command("recover", help="Requeue a completed task without reverting workspace code")
def recover_command(
    task_id: Annotated[str, typer.Argument(help="Task id to recover")],
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    """
    Typer wrapper around :func:`recover`.

    Kept separate so the helper can be invoked from inside other
    queue commands (notably ``requeue`` when it detects an
    already-done task) without going through the Typer command
    object.
    """
    return recover(task_id, workspace)


EngineChoice = choice(ENGINE_CHOICES)


@app.command("switch", help="Switch a queued or active task to a different engine")
def switch(
    task_id: Annotated[str, typer.Argument(help="Task id to switch")],
    engine: Annotated[str, typer.Argument(click_type=EngineChoice, help="Engine to switch to")],
    reason: Annotated[str, typer.Option(help="Why the engine switch happened")],
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    """
    Reassign a task's engine, interrupting the active runner when needed.

    The required ``--reason`` is captured in the journal so engine
    churn is auditable; without that audit trail it would be hard
    to diagnose why a task drifted between engines mid-run. If the
    task is active the runner receives a SIGTERM so it stops
    before producing more work on the old engine.
    """
    create_workspace(workspace)
    workspace_obj = build_workspace(workspace)
    try:
        summary = switch_task_engine_for_workspace(workspace_obj, task_id, engine=engine, reason=reason)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"switch failed: {exc}")
        return 1
    state = load_state_for_workspace(workspace_obj)
    print(f"task: {summary.task.id} {summary.task.title}")
    print("status: queued")
    print(f"pipeline_stage: {summary.task.pipeline_status}")
    print(f"pipeline_status: {summary.task.pipeline_status}")
    if summary.was_active:
        was_active_label = "yes"
    else:
        was_active_label = "no"
    if summary.runner_pid is not None:
        runner_pid_label = summary.runner_pid
    else:
        runner_pid_label = "-"
    if summary.signal_sent:
        signal_sent_label = "yes"
    else:
        signal_sent_label = "no"
    print(f"engine: {summary.previous_engine} -> {summary.new_engine}")
    print(f"was_active: {was_active_label}")
    print(f"runner_pid: {runner_pid_label}")
    print(f"signal_sent: {signal_sent_label}")
    print(f"position: {state.queue.index(summary.task.id) + 1}")
    return 0
