import sys

from litehive.config import ensure_workspace
from litehive.db import apply_pending_migrations
from litehive.daemon import (
    daemon_status_lines,
    list_daemon_instances,
    run_daemon_loop,
    start_background_daemon,
    stop_workspace_daemon,
)


def _cmd_daemon_run(args):
    ensure_workspace(args.workspace)
    apply_pending_migrations(args.workspace)
    if getattr(args, "foreground", False):
        return run_daemon_loop(args.workspace, output_stream=sys.stdout)
    try:
        pid = start_background_daemon(args.workspace)
    except RuntimeError as exc:
        print(f"daemon run failed: {exc}")
        return 1
    print(f"workspace: {args.workspace.resolve()}")
    print(f"daemon_status: running")
    print(f"pid: {pid}")
    return 0


def _cmd_daemon_status(args):
    ensure_workspace(args.workspace)
    for line in daemon_status_lines(args.workspace):
        print(line)
    return 0


def _cmd_daemon_stop(args):
    ensure_workspace(args.workspace)
    entry = stop_workspace_daemon(args.workspace)
    print(f"workspace: {args.workspace.resolve()}")
    if entry is None:
        print("daemon_status: stopped")
        print("stopped: no")
        return 1
    print(f"pid: {entry.get('pid')}")
    print("daemon_status: stopped")
    print("stopped: yes")
    return 0


def _cmd_daemon_restart(args):
    ensure_workspace(args.workspace)
    previous = stop_workspace_daemon(args.workspace)
    try:
        pid = start_background_daemon(args.workspace)
    except RuntimeError as exc:
        print(f"daemon restart failed: {exc}")
        return 1
    print(f"workspace: {args.workspace.resolve()}")
    print(f"previous_pid: {previous.get('pid') if previous is not None else '-'}")
    print(f"pid: {pid}")
    print("daemon_status: running")
    return 0


def _cmd_daemon_instances(_):
    instances = list_daemon_instances()
    print(f"instances: {len(instances)}")
    for index, entry in enumerate(instances, start=1):
        print(
            f"{index}. workspace={entry.get('workspace')} pid={entry.get('pid')} "
            f"started_at={entry.get('started_at')} log_dir={entry.get('log_dir')}"
        )
    return 0


def _cmd_daemon_worker(args):
    ensure_workspace(args.workspace)
    apply_pending_migrations(args.workspace)
    return run_daemon_loop(args.workspace, output_stream=None)
