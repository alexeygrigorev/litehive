"""Live task recovery helpers that are still exposed via the CLI."""

from pathlib import Path

from litehive.config.model import LitehiveConfig
from litehive.config.paths import state_path
from litehive.git.ops import GitError, abort_revert, commit_task, has_changes, rollback_message, rollback_task
from litehive.domain.task import TaskRecord
from litehive.state.store import runtime_store
from litehive.state.persist import atomic_write_text, load_state
from litehive.tasks.paths import task_dir, task_file
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.queue import prepare_completed_task_for_recovery
from litehive.state.locking import workspace_lock, workspace_mutation_guard
from litehive.state.persist import persist_task_and_state


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


def rollback_completed_task(root: Path, task_id: str):
    from litehive.domain.pool import RollbackSummary
    from litehive.state.records import get_task

    root = root.resolve()
    with workspace_mutation_guard(root), workspace_lock(root):
        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        require_completed_task(task, action="rollback")
        attempt = task.git.checkpoint_attempts
        recovery_stage = implementation_entry_stage(task)
        state = load_state(root)
        file_snapshot = _capture_persisted_files(
            [
                task_file(root, task),
                state_path(root),
                task_dir(root, task) / "journal.md",
            ]
        )
        runtime_snapshot = _capture_runtime_snapshot(root, task.id)
        rollback = None
        try:
            rollback = rollback_task(root, task)
            prepare_completed_task_for_recovery(task, recovery_stage=recovery_stage)
            task.git.rolled_back_checkpoint_attempt = attempt
            state.active_task_id = None
            state.queue = [item for item in state.queue if item != task.id]
            state.queue.append(task.id)
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message="Checkpoint rollback requested.\n"
                f"- rolled_back_attempt: `{attempt}`\n"
                f"- recovery_stage: `{recovery_stage}`",
            )
            rollback_checkpoint = commit_task(root, rollback_message(task, attempt))
            if rollback_checkpoint is None:
                raise GitError("git rollback commit failed")
        except Exception:
            if rollback is not None and has_changes(root):
                abort_revert(root)
            _restore_persisted_files(file_snapshot)
            _restore_runtime_snapshot(root, task_id=task.id, runtime_snapshot=runtime_snapshot)
            raise
        return RollbackSummary(
            task=task,
            rollback_sha=rollback_checkpoint.commit_sha,
            rolled_back_sha=rollback.rolled_back_sha,
        )


def recover_completed_task(root: Path, task_id: str) -> TaskRecord:
    root = root.resolve()
    with workspace_mutation_guard(root), workspace_lock(root):
        from litehive.state.records import get_task

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


def _capture_persisted_files(paths: list[Path]) -> dict[Path, str | None]:
    snapshot: dict[Path, str | None] = {}
    for path in paths:
        snapshot[path] = path.read_text(encoding="utf-8") if path.exists() else None
    return snapshot


def _restore_persisted_files(snapshot: dict[Path, str | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            if path.exists():
                path.unlink()
            continue
        atomic_write_text(path, content)


def _capture_runtime_snapshot(root: Path, task_id: str):
    store = runtime_store(root)
    return store.load_workspace_state(), store.load_task_state(task_id)


def _restore_runtime_snapshot(root: Path, *, task_id: str, runtime_snapshot) -> None:
    workspace_state, task_state = runtime_snapshot
    store = runtime_store(root)
    if workspace_state is not None:
        store.save_workspace_state(workspace_state)
    if task_state is not None:
        store.save_task_state(task_id, task_state)
