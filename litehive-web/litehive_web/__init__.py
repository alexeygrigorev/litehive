"""Local-only HTTP monitor for queue, task, and session artifacts."""

from litehive.daemon import (
    get_workspace_daemon,
    start_background_daemon,
    stop_workspace_daemon,
)
from litehive_web.actions import (
    abandon_task_via_web,
    close_task_via_web,
    create_task_via_web,
    requeue_task_via_web,
    stop_active_task_via_web,
    submit_stage_verdict_via_web,
    switch_task_engine_via_web,
    update_default_engine,
    update_task_detail,
    update_task_via_web,
)
from litehive_web.common import render_index
from litehive_web.server import LitehiveWebHandler, WorkspaceStreamMonitor, serve_monitor
from litehive_web.snapshot import (
    build_daemon_status_payload,
    build_workspace_snapshot,
    list_recent_run_all_logs,
    read_engine_dashboard,
    read_session_view,
)

__all__ = [
    "render_index",
    "LitehiveWebHandler",
    "WorkspaceStreamMonitor",
    "serve_monitor",
    "build_daemon_status_payload",
    "build_workspace_snapshot",
    "get_workspace_daemon",
    "list_recent_run_all_logs",
    "read_engine_dashboard",
    "read_session_view",
    "start_background_daemon",
    "stop_workspace_daemon",
    "submit_stage_verdict_via_web",
    "switch_task_engine_via_web",
    "update_default_engine",
    "update_task_detail",
    "create_task_via_web",
    "update_task_via_web",
    "close_task_via_web",
    "requeue_task_via_web",
    "abandon_task_via_web",
    "stop_active_task_via_web",
]
