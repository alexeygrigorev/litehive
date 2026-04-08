"""Local-only HTTP monitor for queue, task, and session artifacts."""

from litehive.web.common import _render_index
from litehive.web.snapshot import (
    build_workspace_snapshot,
    list_recent_run_all_logs,
    read_engine_dashboard,
    read_session_view,
)
from litehive.web.actions import (
    submit_stage_verdict_via_web,
    switch_task_engine_via_web,
    update_default_engine,
    update_task_detail,
)
from litehive.web.server import (
    LitehiveWebHandler,
    WorkspaceStreamMonitor,
    serve_monitor,
)

__all__ = [
    "serve_monitor",
    "build_workspace_snapshot",
    "read_session_view",
]
