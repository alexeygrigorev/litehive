"""CLI entrypoint for litehive."""

from __future__ import annotations

import argparse
from pathlib import Path

from litehive.config import LitehiveConfig, ensure_workspace, load_config
from litehive.git_ops import GitError
from litehive.runtime import (
    recover_completed_task,
    resolve_next_task,
    rollback_completed_task,
    run_next_task,
)
from litehive.tasks import create_task, list_tasks, load_state
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

    status = subparsers.add_parser("status", help="Show workspace status")
    status.add_argument(
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
    add.add_argument("--engine", choices=["codex", "opencode"], help="Preferred engine for the task")
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

    run = subparsers.add_parser("run", help="Run the active or next queued task")
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

    return parser


def _cmd_configure(args: argparse.Namespace) -> int:
    config = LitehiveConfig(
        default_engine=args.default_engine,
        opencode_model=args.opencode_model,
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
    print(f"mode: {state.mode}")
    print(f"active_task_id: {state.active_task_id}")
    print(f"queued_tasks: {len(state.queue)}")
    if tasks:
        print()
        for task in tasks:
            marker = "*" if task.id == state.active_task_id else " "
            print(
                f"{marker} {task.id} [{task.status}/{task.pipeline_status}] "
                f"{task.mode} {task.title}"
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
    summary = run_next_task(args.workspace)
    if summary.task is None:
        print("No queued task.")
        return 0
    print(f"task: {summary.task.id} {summary.task.title}")
    if summary.result is not None:
        print(f"status: {summary.result.final_status}")
        print(f"steps: {summary.result.steps_executed}")
        print(f"last_verdict: {summary.result.last_verdict}")
    if summary.commit_sha:
        print(f"commit: {summary.commit_sha}")
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


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "configure":
        return _cmd_configure(args)
    if args.command == "status":
        return _cmd_status(args)
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

    summary = run_next_task(Path.cwd())
    if summary.task is not None:
        if summary.result is not None:
            print(f"{summary.task.id}: {summary.result.final_status}")
        return 0
    return _launch_app(Path.cwd(), default_mode="implementation")


if __name__ == "__main__":
    raise SystemExit(main())
