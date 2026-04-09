"""Local-only HTTP monitor for queue, task, and session artifacts."""

from litehive.web.actions import (
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
from litehive.web.common import _render_index
from litehive.web.server import LitehiveWebHandler, WorkspaceStreamMonitor, serve_monitor
from litehive.web.snapshot import (
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
    "build_workspace_snapshot",
    "list_recent_run_all_logs",
    "read_engine_dashboard",
    "read_session_view",
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
