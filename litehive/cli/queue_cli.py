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
from litehive.config.loading import load_config
from litehive.config.workspace import ensure_workspace
from litehive.git.ops import GitError, checkpoint_message
from litehive.recovery.workspace_repair import recover_stale_runner_state
from litehive.recovery.execution_recovery import recover_completed_task
from litehive.state.records import list_tasks, require_task
from litehive.tasks.models import WorkspaceConflictError
from litehive.tasks.normalization import missing_acceptance_criteria_reason
from litehive.tasks.persistence import load_state
from litehive.tasks.queue import move_queued_task, prioritize_queued_tasks
from litehive.tasks.status import (
    requeue_task,
    resume_task,
    stop_current_task,
    switch_task_engine,
)

app = make_typer(invoke_without_command=True)


@app.callback(invoke_without_command=True)
def queue_group(ctx: typer.Context, workspace: WorkspaceOption = Path.cwd()) -> int | None:
    if ctx.invoked_subcommand is not None:
        return None
    config = load_config(workspace)
    recover_stale_runner_state(workspace)
    state = load_state(workspace)
    tasks = list_tasks(workspace)
    print(f"active_task_id: {state.active_task_id}")
    if state.active_task_id is not None:
        active_task = require_task(workspace, state.active_task_id)
        print(
            f"active: {active_task.id} [{active_task.status}/{active_task.pipeline_status}] "
            f"priority={active_task.priority} engine={task_engine_label(None, config.default_engine)} "
            f"model={task_model_label(active_task.model)} "
            f"title={active_task.title} depends_on={task_dependencies_label(active_task.id, active_task.depends_on)}"
            f"{task_interruption_label(active_task)}"
        )
    print(f"queue_length: {len(state.queue)}")
    for index, queued_task_id in enumerate(state.queue, start=1):
        task = require_task(workspace, queued_task_id)
        print(
            f"{index}. {task.id} [{task.status}/{task.pipeline_status}] "
            f"priority={task.priority} engine={task_engine_label(None, config.default_engine)} "
            f"model={task_model_label(task.model)} "
            f"title={task.title} depends_on={task_dependencies_label(task.id, task.depends_on)}"
            f"{task_interruption_label(task)}"
        )
    resumable = [task for task in tasks if task.status in {"interrupted", "parked"}]
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
    ensure_workspace(workspace)
    try:
        state = move_queued_task(workspace, task_id, position)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"move failed: {exc}")
        return 1
    print(f"task_id: {task_id}")
    print(f"position: {state.queue.index(task_id) + 1}")
    return 0


@app.command("promote", help="Move a queued task to the front of the queue")
def promote(task_id: Annotated[str, typer.Argument(help="Queued task id")], workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    try:
        task = require_task(workspace, task_id)
        if task.status in {"interrupted", "parked", "flagged", "cancelled", "wont_do", "deferred", "duplicate"}:
            task = resume_task(workspace, task_id, front=True)
            print(f"task: {task.id} {task.title}")
            print("status: queued")
            print(f"pipeline_status: {task.pipeline_status}")
            missing_criteria_reason = missing_acceptance_criteria_reason(task)
            if missing_criteria_reason is not None:
                print(f"warning: {missing_criteria_reason}")
            print("position: 1")
            return 0
        move_queued_task(workspace, task_id, 1)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"promote failed: {exc}")
        return 1
    print(f"task_id: {task_id}")
    print("position: 1")
    return 0


@app.command("requeue", help="Requeue a flagged or closed task")
def requeue(
    task_id: Annotated[str, typer.Argument(help="Task id to requeue")],
    workspace: WorkspaceOption = Path.cwd(),
    front: Annotated[bool, typer.Option(help="Insert at the front of the queue")] = False,
    force: Annotated[bool, typer.Option(help="Force requeue after repeated flagging")] = False,
) -> int:
    ensure_workspace(workspace)
    task = require_task(workspace, task_id)
    if task.pipeline_status == "done" or task.status == "done":
        return recover(task_id, workspace)
    try:
        task = requeue_task(workspace, task_id, front=front, force=force)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"requeue failed: {exc}")
        return 1
    state = load_state(workspace)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


@app.command("resume", help="Resume an interrupted, parked, flagged, or closed task")
def resume(
    task_id: Annotated[str, typer.Argument(help="Task id to resume")],
    workspace: WorkspaceOption = Path.cwd(),
    front: Annotated[bool, typer.Option(help="Insert at the front of the queue")] = False,
) -> int:
    ensure_workspace(workspace)
    try:
        task = resume_task(workspace, task_id, front=front)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"resume failed: {exc}")
        return 1
    state = load_state(workspace)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


@app.command("stop", help="Stop the current active task cleanly")
def stop(workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    try:
        summary = stop_current_task(workspace)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"stop failed: {exc}")
        return 1
    print(f"task: {summary.task.id} {summary.task.title}")
    print(f"status: {summary.task.status}")
    print(f"pipeline_status: {summary.task.pipeline_status}")
    print(f"runner_pid: {summary.runner_pid if summary.runner_pid is not None else '-'}")
    print(f"signal_sent: {'yes' if summary.signal_sent else 'no'}")
    return 0


def prioritize(task_ids: list[str], workspace: Path) -> int:
    ensure_workspace(workspace)
    try:
        state = prioritize_queued_tasks(workspace, task_ids)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"prioritize failed: {exc}")
        return 1
    print(f"moved_tasks: {' '.join(task_ids)}")
    print(f"moved_count: {len(task_ids)}")
    print(f"front_of_queue: {' '.join(state.queue[: len(task_ids)])}")
    print(f"queue_length: {len(state.queue)}")
    return 0


def recover(task_id: str, workspace: Path) -> int:
    ensure_workspace(workspace)
    try:
        task = recover_completed_task(workspace, task_id)
    except (GitError, WorkspaceConflictError) as exc:
        print(f"recover failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    print("recovery_policy: recover requeued the task without reverting workspace code")
    print(f"next_commit_message: {checkpoint_message(task)}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def switch(task_id: str, engine: str, workspace: Path, reason: str) -> int:
    ensure_workspace(workspace)
    try:
        summary = switch_task_engine(workspace, task_id, engine=engine, reason=reason)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"switch failed: {exc}")
        return 1
    state = load_state(workspace)
    print(f"task: {summary.task.id} {summary.task.title}")
    print("status: queued")
    print(f"pipeline_status: {summary.task.pipeline_status}")
    print(f"engine: {summary.previous_engine} -> {summary.new_engine}")
    print(f"was_active: {'yes' if summary.was_active else 'no'}")
    print(f"runner_pid: {summary.runner_pid if summary.runner_pid is not None else '-'}")
    print(f"signal_sent: {'yes' if summary.signal_sent else 'no'}")
    print(f"position: {state.queue.index(summary.task.id) + 1}")
    return 0


EngineChoice = choice(ENGINE_CHOICES)


def register_hidden_root_commands(app: typer.Typer) -> None:
    @app.command(
        "recover",
        help="Use after an accepted task needs another pass but its current code should stay in place",
    )
    def recover_command(
        task_id: Annotated[str, typer.Argument(help="Task id to recover")],
        workspace: WorkspaceOption = Path.cwd(),
    ) -> int:
        return recover(task_id, workspace)

    @app.command(
        "prioritize",
        help="Use to pull queued tasks to the front when operator ordering matters more than the current queue",
    )
    def prioritize_command(
        task_ids: Annotated[list[str], typer.Argument(help="Queued task ids to move to the front")],
        workspace: WorkspaceOption = Path.cwd(),
    ) -> int:
        return prioritize(task_ids, workspace)

    @app.command(
        "switch",
        help="Use when a task should continue with a different engine on its next queued run",
    )
    def switch_command(
        task_id: Annotated[str, typer.Argument(help="Task id to switch")],
        engine: Annotated[str, typer.Argument(click_type=EngineChoice, help="Engine to switch to")],
        workspace: WorkspaceOption = Path.cwd(),
        reason: Annotated[str, typer.Option(help="Why the engine switch happened")] = ...,
    ) -> int:
        return switch(task_id, engine, workspace, reason)
