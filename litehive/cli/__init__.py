"""CLI entrypoint for litehive."""

from pathlib import Path

from litehive.pipeline import run_next_task
from litehive.web import serve_monitor

from litehive.cli.configure import _cmd_configure
from litehive.cli.debug import _cmd_debug
from litehive.cli.doctor import _cmd_doctor
from litehive.cli.daemon import (
    _cmd_daemon_instances,
    _cmd_daemon_restart,
    _cmd_daemon_run,
    _cmd_daemon_status,
    _cmd_daemon_stop,
    _cmd_daemon_worker,
)
from litehive.cli.engine import _cmd_engine
from litehive.cli.health import _cmd_health
from litehive.cli.queue import (
    _cmd_abandon_task,
    _cmd_archive,
    _cmd_cleanup,
    _cmd_close_task,
    _cmd_dirty_worktree_gate,
    _cmd_move,
    _cmd_prioritize,
    _cmd_promote,
    _cmd_queue_requeue,
    _cmd_recover,
    _cmd_requeue_task,
    _cmd_resume_task,
    _cmd_rollback,
    _cmd_stop_task,
    _cmd_switch_task,
    _launch_app,
)
from litehive.cli.github_import import _cmd_import_github, _cmd_import_issue, _cmd_import_issues
from litehive.cli.logs import _cmd_logs
from litehive.cli.report import _cmd_report
from litehive.cli.run import _cmd_run
from litehive.cli.status import _cmd_list, _cmd_queue, _cmd_repair, _cmd_show, _cmd_status
from litehive.cli.tasks import _cmd_add, _cmd_intake, _cmd_issue, _cmd_update
from litehive.cli.worktree import _cmd_worktree_clean, _cmd_worktree_ls
from litehive.cli.parser import build_parser


_COMMAND_HANDLERS = {
    "configure": _cmd_configure,
    "status": _cmd_status,
    "doctor": _cmd_doctor,
    "health": _cmd_health,
    "engine": _cmd_engine,
    "queue": _cmd_queue,
    "repair": _cmd_repair,
    "add": _cmd_add,
    "issue": _cmd_issue,
    "intake": _cmd_intake,
    "run": _cmd_run,
    "dirty-worktree-gate": _cmd_dirty_worktree_gate,
    "rollback": _cmd_rollback,
    "recover": _cmd_recover,
    "move": _cmd_move,
    "prioritize": _cmd_prioritize,
    "promote": _cmd_promote,
    "requeue": _cmd_requeue_task,
    "resume": _cmd_resume_task,
    "abandon": _cmd_abandon_task,
    "stop": _cmd_stop_task,
    "switch": _cmd_switch_task,
    "close": _cmd_close_task,
    "archive": _cmd_archive,
    "cleanup": _cmd_cleanup,
    "list": _cmd_list,
    "show": _cmd_show,
    "import-issue": _cmd_import_issue,
    "import-issues": _cmd_import_issues,
    "debug": _cmd_debug,
    "logs": _cmd_logs,
    "update": _cmd_update,
    "report": _cmd_report,
}

_DAEMON_COMMAND_HANDLERS = {
    "run": _cmd_daemon_run,
    "status": _cmd_daemon_status,
    "stop": _cmd_daemon_stop,
    "restart": _cmd_daemon_restart,
    "instances": _cmd_daemon_instances,
    "worker": _cmd_daemon_worker,
}

_WORKTREE_COMMAND_HANDLERS = {
    "ls": _cmd_worktree_ls,
    "clean": _cmd_worktree_clean,
}

_TASK_COMMAND_HANDLERS = {
    "add": _cmd_add,
    "list": _cmd_list,
    "show": _cmd_show,
    "update": _cmd_update,
    "close": _cmd_close_task,
    "abandon": _cmd_abandon_task,
    "debug": _cmd_debug,
    "logs": _cmd_logs,
}

_QUEUE_COMMAND_HANDLERS = {
    "move": _cmd_move,
    "promote": _cmd_promote,
    "requeue": _cmd_queue_requeue,
    "resume": _cmd_resume_task,
    "stop": _cmd_stop_task,
}

_IMPORT_COMMAND_HANDLERS = {
    "github": _cmd_import_github,
    "issue": _cmd_issue,
    "spec": _cmd_intake,
}

_ARCHIVE_COMMAND_HANDLERS = {
    "cleanup": _cmd_cleanup,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "tasks":
        return _launch_app(args.workspace, default_mode="tasks")
    if args.command == "web":
        return serve_monitor(args.workspace, host=args.host, port=args.port)
    if args.command == "start":
        return _cmd_daemon_run(args)
    if args.command == "stop":
        return _cmd_daemon_stop(args)
    if args.command == "restart":
        return _cmd_daemon_restart(args)
    if args.command == "daemon":
        handler = _DAEMON_COMMAND_HANDLERS.get(getattr(args, "daemon_command", None))
        if handler is None:
            parser.error("daemon requires a subcommand")
        return handler(args)
    if args.command == "task":
        handler = _TASK_COMMAND_HANDLERS.get(getattr(args, "task_command", None))
        if handler is None:
            parser.error("task requires a subcommand")
        return handler(args)
    if args.command == "queue":
        handler = _QUEUE_COMMAND_HANDLERS.get(getattr(args, "queue_command", None))
        if handler is None:
            return _cmd_queue(args)
        return handler(args)
    if args.command == "import":
        handler = _IMPORT_COMMAND_HANDLERS.get(getattr(args, "import_command", None))
        if handler is None:
            parser.error("import requires a subcommand")
        return handler(args)
    if args.command == "archive":
        handler = _ARCHIVE_COMMAND_HANDLERS.get(getattr(args, "archive_command", None))
        if handler is not None:
            return handler(args)
        return _cmd_archive(args)
    if args.command == "worktree":
        handler = _WORKTREE_COMMAND_HANDLERS.get(getattr(args, "worktree_command", None))
        if handler is None:
            parser.error("worktree requires a subcommand")
        return handler(args)

    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is not None:
        return handler(args)

    summary = run_next_task(Path.cwd())
    if summary.task is not None:
        if summary.result is not None:
            print(f"{summary.task.id}: {summary.result.final_status}")
        return 0
    return _launch_app(Path.cwd(), default_mode="implementation")


if __name__ == "__main__":
    raise SystemExit(main())
