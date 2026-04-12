"""Curated public task API."""

from importlib import import_module

_EXPORTS = {
    "VALID_HUMAN_CHECKPOINTS": (".constants", "VALID_HUMAN_CHECKPOINTS"),
    "VALID_PLANNED_EFFORTS": (".constants", "VALID_PLANNED_EFFORTS"),
    "VALID_PM_COMPLEXITIES": (".constants", "VALID_PM_COMPLEXITIES"),
    "VALID_TASK_ENGINES": (".constants", "VALID_TASK_ENGINES"),
    "VALID_TASK_PRIORITIES": (".constants", "VALID_TASK_PRIORITIES"),
    "VALID_TASK_TYPES": (".constants", "VALID_TASK_TYPES"),
    "WorkspaceConflictError": (".models", "WorkspaceConflictError"),
    "append_journal": (".journal", "append_journal"),
    "append_thread_comment": (".reports", "append_thread_comment"),
    "create_task": (".crud", "create_task"), "get_task": (".crud", "get_task"),
    "list_tasks": (".crud", "list_tasks"), "list_tasks_state_first": (".crud", "list_tasks_state_first"),
    "load_state": (".persistence", "load_state"), "load_task_thread": (".reports", "load_task_thread"),
    "render_task_thread": (".reports", "render_task_thread"), "require_task": (".crud", "require_task"),
    "save_state": (".persistence", "save_state"), "save_task": (".crud", "save_task"),
    "set_active_task": (".queue_ops", "set_active_task"),
    "slugify": (".paths", "slugify"), "task_dir": (".paths", "task_dir"), "task_file": (".paths", "task_file"),
}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    return getattr(import_module(module_name, __name__), attr_name)
