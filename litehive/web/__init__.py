"""Local-only HTTP monitor for queue, task, and session artifacts."""

from litehive.daemon import (
    get_workspace_daemon,
    start_background_daemon,
    stop_workspace_daemon,
)
from litehive.web.actions import (
    submit_stage_verdict_via_web,
    switch_task_engine_via_web,
    update_default_engine,
    update_task_detail,
)
from litehive.web.common import _render_index
from litehive.web.server import LitehiveWebHandler, WorkspaceStreamMonitor, serve_monitor
from litehive.web.snapshot import (
    build_daemon_status_payload,
    build_workspace_snapshot,
    list_recent_run_all_logs,
    read_engine_dashboard,
    read_session_view,
)

__all__ = [
    "_render_index",
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
]
