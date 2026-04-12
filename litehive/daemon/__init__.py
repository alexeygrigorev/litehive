"""Daemon lifecycle helpers for Litehive pool execution."""

from litehive.daemon.execution import (
    daemon_status_lines,
    run_daemon_loop,
    start_background_daemon,
    stop_workspace_daemon,
)
from litehive.daemon.logs import (
    prune_run_all_log_dirs,
    latest_run_all_log_dir,
)
from litehive.daemon.registry import (
    get_workspace_daemon,
    list_daemon_instances,
    register_daemon,
    unregister_daemon,
)

__all__ = [
    "daemon_status_lines",
    "get_workspace_daemon",
    "list_daemon_instances",
    "latest_run_all_log_dir",
    "register_daemon",
    "run_daemon_loop",
    "start_background_daemon",
    "stop_workspace_daemon",
    "unregister_daemon",
    "prune_run_all_log_dirs",
]
