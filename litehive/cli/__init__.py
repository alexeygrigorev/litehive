"""CLI entrypoint for litehive."""

from pathlib import Path

from litehive.runtime import run_next_task
from litehive.web import serve_monitor

from litehive.cli.configure import _cmd_configure
from litehive.cli.debug import _cmd_debug
from litehive.cli.daemon import (
    _cmd_daemon_instances,
    _cmd_daemon_restart,
    _cmd_daemon_run,
    _cmd_daemon_status,
    _cmd_daemon_stop,
    _cmd_daemon_worker,
)
from litehive.cli.engine import _cmd_engine
from litehive.cli.queue import (
    _cmd_abandon_task,
    _cmd_archive,
    _cmd_cleanup,
    _cmd_close_task,
    _cmd_dirty_worktree_gate,
    _cmd_move,
    _cmd_prioritize,
    _cmd_promote,
    _cmd_recover,
    _cmd_requeue_task,
    _cmd_resume_task,
    _cmd_rollback,
    _cmd_stop_task,
    _cmd_switch_task,
    _launch_app,
)
from litehive.cli.github_import import _cmd_import_issue, _cmd_import_issues
from litehive.cli.report import _cmd_report
from litehive.cli.run import _cmd_run
from litehive.cli.status import _cmd_list, _cmd_queue, _cmd_repair, _cmd_show, _cmd_status
from litehive.cli.github_import import _cmd_import_issue, _cmd_import_issues
from litehive.cli.tasks import _cmd_add, _cmd_intake, _cmd_issue, _cmd_update
from litehive.cli.parser import build_parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "configure":
        return _cmd_configure(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "engine":
        return _cmd_engine(args)
    if args.command == "queue":
        return _cmd_queue(args)
    if args.command == "repair":
        return _cmd_repair(args)
    if args.command == "tasks":
        return _launch_app(args.workspace, default_mode="tasks")
    if args.command == "web":
        return serve_monitor(args.workspace, host=args.host, port=args.port)
    if args.command == "daemon":
        if args.daemon_command == "run":
            return _cmd_daemon_run(args)
        if args.daemon_command == "status":
            return _cmd_daemon_status(args)
        if args.daemon_command == "stop":
            return _cmd_daemon_stop(args)
        if args.daemon_command == "restart":
            return _cmd_daemon_restart(args)
        if args.daemon_command == "instances":
            return _cmd_daemon_instances(args)
        if args.daemon_command == "worker":
            return _cmd_daemon_worker(args)
        parser.error("daemon requires a subcommand")
    if args.command == "add":
        return _cmd_add(args)
    if args.command == "issue":
        return _cmd_issue(args)
    if args.command == "intake":
        return _cmd_intake(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "dirty-worktree-gate":
        return _cmd_dirty_worktree_gate(args)
    if args.command == "rollback":
        return _cmd_rollback(args)
    if args.command == "recover":
        return _cmd_recover(args)
    if args.command == "move":
        return _cmd_move(args)
    if args.command == "prioritize":
        return _cmd_prioritize(args)
    if args.command == "promote":
        return _cmd_promote(args)
    if args.command == "requeue":
        return _cmd_requeue_task(args)
    if args.command == "resume":
        return _cmd_resume_task(args)
    if args.command == "abandon":
        return _cmd_abandon_task(args)
    if args.command == "stop":
        return _cmd_stop_task(args)
    if args.command == "switch":
        return _cmd_switch_task(args)
    if args.command == "close":
        return _cmd_close_task(args)
    if args.command == "archive":
        return _cmd_archive(args)
    if args.command == "cleanup":
        return _cmd_cleanup(args)
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "show":
        return _cmd_show(args)
    if args.command == "import-issue":
        return _cmd_import_issue(args)
    if args.command == "import-issues":
        return _cmd_import_issues(args)
    if args.command == "debug":
        return _cmd_debug(args)
    if args.command == "update":
        return _cmd_update(args)
    if args.command == "import-issue":
        return _cmd_import_issue(args)
    if args.command == "import-issues":
        return _cmd_import_issues(args)
    if args.command == "debug":
        return _cmd_debug(args)
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "show":
        return _cmd_show(args)

    summary = run_next_task(Path.cwd())
    if summary.task is not None:
        if summary.result is not None:
            print(f"{summary.task.id}: {summary.result.final_status}")
        return 0
    return _launch_app(Path.cwd(), default_mode="implementation")


if __name__ == "__main__":
    raise SystemExit(main())
