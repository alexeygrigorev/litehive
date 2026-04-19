"""Local-only HTTP monitor for queue, task, and session artifacts."""

from __future__ import annotations

from importlib import import_module

_EXPORTS: dict[str, tuple[str, str]] = {
    "render_index": ("litehive_web.common", "render_index"),
    "LitehiveWebHandler": ("litehive_web.server", "LitehiveWebHandler"),
    "WorkspaceStreamMonitor": ("litehive_web.server", "WorkspaceStreamMonitor"),
    "serve_monitor": ("litehive_web.server", "serve_monitor"),
    "build_daemon_status_payload": ("litehive_web.snapshot", "build_daemon_status_payload"),
    "build_workspace_snapshot": ("litehive_web.snapshot", "build_workspace_snapshot"),
    "get_workspace_daemon": ("litehive.daemon", "get_workspace_daemon"),
    "list_recent_run_all_logs": ("litehive_web.snapshot", "list_recent_run_all_logs"),
    "read_engine_dashboard": ("litehive_web.snapshot", "read_engine_dashboard"),
    "read_session_view": ("litehive_web.snapshot", "read_session_view"),
    "start_background_daemon": ("litehive.daemon", "start_background_daemon"),
    "stop_workspace_daemon": ("litehive.daemon", "stop_workspace_daemon"),
    "submit_stage_verdict_via_web": ("litehive_web.actions", "submit_stage_verdict_via_web"),
    "switch_task_engine_via_web": ("litehive_web.actions", "switch_task_engine_via_web"),
    "update_default_engine": ("litehive_web.actions", "update_default_engine"),
    "update_task_detail": ("litehive_web.actions", "update_task_detail"),
    "create_task_via_web": ("litehive_web.actions", "create_task_via_web"),
    "update_task_via_web": ("litehive_web.actions", "update_task_via_web"),
    "close_task_via_web": ("litehive_web.actions", "close_task_via_web"),
    "requeue_task_via_web": ("litehive_web.actions", "requeue_task_via_web"),
    "abandon_task_via_web": ("litehive_web.actions", "abandon_task_via_web"),
    "stop_active_task_via_web": ("litehive_web.actions", "stop_active_task_via_web"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
