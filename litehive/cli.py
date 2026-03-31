"""CLI entrypoint for litehive."""

from __future__ import annotations

import argparse
from pathlib import Path

from litehive.config import LitehiveConfig, ensure_workspace, load_config
from litehive.git_ops import GitError
from litehive.observability import render_task_summary
from litehive.runtime import (
    recover_completed_task,
    resolve_next_task,
    rollback_completed_task,
    run_task_pool,
)
from litehive.tasks import (
    VALID_TASK_PRIORITIES,
    create_task,
    list_tasks,
    load_state,
    move_queued_task,
    requeue_task,
    require_task,
    update_task_metadata,
)
from litehive.tui.app import LitehiveApp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="litehive")
    subparsers = parser.add_subparsers(dest="command")

    configure = subparsers.add_parser("configure", help="Initialize litehive workspace config")
    configure.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root where .litehive/ should be created",
    )
    configure.add_argument(
        "--default-engine",
        default="codex",
        help="Default engine adapter name",
    )
    configure.add_argument(
        "--opencode-model",
        default="zai-coding-plan/glm-5.1",
        help="Default model identifier when using the opencode adapter",
    )
    configure.add_argument(
        "--gemini-model",
        default=None,
        help="Default model identifier when using the gemini adapter",
    )

    status = subparsers.add_parser("status", help="Show workspace status")
    status.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    queue = subparsers.add_parser("queue", help="Show the active task and queued order")
    queue.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    tasks = subparsers.add_parser("tasks", help="Open the task view")
    tasks.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    add = subparsers.add_parser("add", help="Create a queued task")
    add.add_argument("title", help="Task title")
    add.add_argument("--goal", default="", help="Task goal text")
    add.add_argument("--engine", choices=["codex", "opencode", "gemini"], help="Preferred engine for the task")
    add.add_argument(
        "--no-auto-commit",
        action="store_true",
        help="Disable auto-commit for this task",
    )
    add.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    run = subparsers.add_parser("run", help="Drain the active and queued task pool")
    run.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which task and engine would run without invoking an agent",
    )

    rollback = subparsers.add_parser("rollback", help="Revert a task checkpoint commit and requeue the task")
    rollback.add_argument("task_id", help="Task id to roll back")
    rollback.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    recover = subparsers.add_parser("recover", help="Requeue a completed task without reverting code")
    recover.add_argument("task_id", help="Task id to recover")
    recover.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    move = subparsers.add_parser("move", help="Move a queued task to a 1-based position")
    move.add_argument("task_id", help="Queued task id to move")
    move.add_argument("position", type=int, help="Target queue position (1-based)")
    move.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    promote = subparsers.add_parser("promote", help="Move a queued task to the front of the queue")
    promote.add_argument("task_id", help="Queued task id to promote")
    promote.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    requeue = subparsers.add_parser("requeue", help="Requeue a flagged or cancelled task")
    requeue.add_argument("task_id", help="Task id to requeue")
    requeue.add_argument(
        "--front",
        action="store_true",
        help="Insert the task at the front of the queue",
    )
    requeue.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    update = subparsers.add_parser("update", help="Update task engine and metadata")
    update.add_argument("task_id", help="Task id to update")
    update.add_argument(
        "--engine",
        choices=["codex", "opencode", "gemini", "default"],
        help="Override task engine, or use 'default' to clear the override",
    )
    update.add_argument(
        "--priority",
        choices=sorted(VALID_TASK_PRIORITIES),
        help="Set task priority",
    )
    update.add_argument("--goal", help="Replace the task goal text")
    update.add_argument(
        "--mode",
        choices=["tasks", "implementation"],
        help="Set task mode",
    )
    update.add_argument(
        "--auto-commit",
        dest="auto_commit",
        action="store_true",
        default=None,
        help="Enable auto-commit for this task",
    )
    update.add_argument(
        "--no-auto-commit",
        dest="auto_commit",
        action="store_false",
        help="Disable auto-commit for this task",
    )
    update.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    return parser


def _cmd_configure(args: argparse.Namespace) -> int:
    config = LitehiveConfig(
        default_engine=args.default_engine,
        opencode_model=args.opencode_model,
        gemini_model=args.gemini_model,
    )
    ensure_workspace(args.workspace, config)
    print(f"Initialized litehive workspace in {args.workspace / '.litehive'}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.workspace)
    state = load_state(args.workspace)
    tasks = list_tasks(args.workspace)
    print(f"workspace: {args.workspace}")
    print(f"default_engine: {config.default_engine}")
    print(f"opencode_model: {config.opencode_model}")
    print(f"gemini_model: {config.gemini_model}")
    print(f"mode: {state.mode}")
    print(f"active_task_id: {state.active_task_id}")
    print(f"queued_tasks: {len(state.queue)}")
    if tasks:
        print()
        for task in tasks:
            for line in render_task_summary(task, active=task.id == state.active_task_id):
                print(line)
    return 0


def _task_engine_label(task_engine: str | None, default_engine: str) -> str:
    return task_engine or f"{default_engine} (default)"


def _cmd_queue(args: argparse.Namespace) -> int:
    config = load_config(args.workspace)
    state = load_state(args.workspace)
    print(f"active_task_id: {state.active_task_id}")
    if state.active_task_id is not None:
        active_task = require_task(args.workspace, state.active_task_id)
        print(
            f"active: {active_task.id} [{active_task.status}/{active_task.pipeline_status}] "
            f"priority={active_task.priority} engine={_task_engine_label(active_task.engine, config.default_engine)} "
            f"title={active_task.title}"
        )
    print(f"queue_length: {len(state.queue)}")
    for index, task_id in enumerate(state.queue, start=1):
        task = require_task(args.workspace, task_id)
        print(
            f"{index}. {task.id} [{task.status}/{task.pipeline_status}] "
            f"priority={task.priority} engine={_task_engine_label(task.engine, config.default_engine)} "
            f"title={task.title}"
        )
    return 0


def _launch_app(workspace: Path, default_mode: str) -> int:
    app = LitehiveApp(workspace=workspace, default_mode=default_mode)
    app.run()
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    if args.dry_run:
        task = resolve_next_task(args.workspace)
        if task is None:
            print("No queued task.")
            return 0
        config = load_config(args.workspace)
        engine_name = task.engine or config.default_engine
        print(f"task: {task.id} {task.title}")
        print(f"engine: {engine_name}")
        return 0
    summary = run_task_pool(args.workspace)
    if not summary.executions:
        print("No queued task.")
        return 0
    for execution in summary.executions:
        if execution.task is None:
            continue
        print(f"task: {execution.task.id} {execution.task.title}")
        if execution.result is not None:
            print(f"status: {execution.result.final_status}")
            print(f"steps: {execution.result.steps_executed}")
            print(f"last_verdict: {execution.result.last_verdict}")
        if execution.commit_sha:
            print(f"commit: {execution.commit_sha}")
    print(f"tasks_run: {len(summary.executions)}")
    print(f"stop_reason: {summary.stop_reason}")
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        summary = rollback_completed_task(args.workspace, args.task_id)
    except GitError as exc:
        print(f"rollback failed: {exc}")
        return 1

    print(f"task: {summary.task.id} {summary.task.title}")
    print(f"rollback_of: {summary.rolled_back_sha}")
    print(f"rollback_commit: {summary.rollback_sha}")
    print("status: queued")
    print("pipeline_status: implementing")
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        task = recover_completed_task(args.workspace, args.task_id)
    except GitError as exc:
        print(f"recover failed: {exc}")
        return 1

    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print("pipeline_status: implementing")
    return 0


def _cmd_move(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        state = move_queued_task(args.workspace, args.task_id, args.position)
    except ValueError as exc:
        print(f"move failed: {exc}")
        return 1
    print(f"task_id: {args.task_id}")
    print(f"position: {state.queue.index(args.task_id) + 1}")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        move_queued_task(args.workspace, args.task_id, 1)
    except ValueError as exc:
        print(f"promote failed: {exc}")
        return 1
    print(f"task_id: {args.task_id}")
    print("position: 1")
    return 0


def _cmd_requeue_task(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        task = requeue_task(args.workspace, args.task_id, front=args.front)
    except ValueError as exc:
        print(f"requeue failed: {exc}")
        return 1
    state = load_state(args.workspace)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print("pipeline_status: implementing")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    if (
        args.engine is None
        and args.priority is None
        and args.goal is None
        and args.mode is None
        and args.auto_commit is None
    ):
        print("update failed: no changes requested")
        return 1
    try:
        task = update_task_metadata(
            args.workspace,
            args.task_id,
            engine=(None if args.engine == "default" else args.engine) if args.engine is not None else ...,
            priority=args.priority if args.priority is not None else ...,
            goal=args.goal if args.goal is not None else ...,
            mode=args.mode if args.mode is not None else ...,
            auto_commit=args.auto_commit if args.auto_commit is not None else ...,
        )
    except ValueError as exc:
        print(f"update failed: {exc}")
        return 1
    config = load_config(args.workspace)
    print(f"task: {task.id} {task.title}")
    print(f"engine: {_task_engine_label(task.engine, config.default_engine)}")
    print(f"priority: {task.priority}")
    print(f"mode: {task.mode}")
    print(f"auto_commit: {task.git.auto_commit}")
    print(f"goal: {task.goal}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "configure":
        return _cmd_configure(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "queue":
        return _cmd_queue(args)
    if args.command == "tasks":
        return _launch_app(args.workspace, default_mode="tasks")
    if args.command == "add":
        ensure_workspace(args.workspace)
        task = create_task(
            args.workspace,
            title=args.title,
            goal=args.goal,
            engine=args.engine,
            auto_commit=not args.no_auto_commit,
        )
        print(f"Created task {task.id} in {args.workspace / '.litehive' / 'tasks' / (task.id + '-' + task.slug)}")
        return 0
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "rollback":
        return _cmd_rollback(args)
    if args.command == "recover":
        return _cmd_recover(args)
    if args.command == "move":
        return _cmd_move(args)
    if args.command == "promote":
        return _cmd_promote(args)
    if args.command == "requeue":
        return _cmd_requeue_task(args)
    if args.command == "update":
        return _cmd_update(args)

    summary = run_next_task(Path.cwd())
    if summary.task is not None:
        if summary.result is not None:
            print(f"{summary.task.id}: {summary.result.final_status}")
        return 0
    return _launch_app(Path.cwd(), default_mode="implementation")


if __name__ == "__main__":
    raise SystemExit(main())
