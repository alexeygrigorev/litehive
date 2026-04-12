"""Live task recovery helpers that are still exposed via the CLI."""

from pathlib import Path

from litehive.config import LitehiveConfig
from litehive.git import GitError
from litehive.models import TaskRecord
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.persistence import load_state
from litehive.tasks.queue_management import prepare_completed_task_for_recovery
from litehive.workspace.locking import workspace_lock, workspace_mutation_guard
from litehive.workspace.workflow import persist_task_and_state


def resolve_recovery_engine(
    root: Path,
    task: TaskRecord,
    config: LitehiveConfig | None,
) -> tuple[str, str | None]:
    from litehive.config.engine_models import resolve_model, select_engine

    model: str | None = None
    if config and config.recovery_engine and config.recovery_engine != "auto":
        engine = config.recovery_engine
    elif config and config.recovery_engine == "auto":
        candidates = list(config.engine_preference) if config.engine_preference else []
        if config.default_engine and config.default_engine not in candidates:
            candidates.append(config.default_engine)
        if not candidates:
            candidates = ["claude", "codex", "copilot", "goz"]
        selection = select_engine(
            root,
            task,
            config,
            engine_names=candidates,
            require_available=True,
        )
        engine = selection.engine_name or config.default_engine or "codex"
        model = selection.model_name if selection.engine_name is not None else None
    else:
        engine = config.default_engine if config else "codex"
    if model is None:
        model = resolve_model(task, config, engine_name=engine) if config else None
    return engine, model


def require_completed_task(task: TaskRecord, action: str) -> None:
    if task.status != "done" or task.pipeline_status != "done":
        raise GitError(f"Task {task.id} is not completed; cannot {action}")


def recover_completed_task(root: Path, task_id: str) -> TaskRecord:
    root = root.resolve()
    with workspace_mutation_guard(root), workspace_lock(root):
        from litehive.tasks.crud import get_task

        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        require_completed_task(task, action="recover")
        recovery_stage = implementation_entry_stage(task)
        prepare_completed_task_for_recovery(task, recovery_stage=recovery_stage)
        state = load_state(root)
        state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        state.queue.append(task.id)
        persist_task_and_state(
            root,
            task=task,
            state=state,
            journal_message="Task recovered for another implementation pass.",
        )
        return task
