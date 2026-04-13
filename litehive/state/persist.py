"""Task state transition and persistence helpers."""

from pathlib import Path

from litehive.config.paths import state_path
from litehive.models.common import utcnow
from litehive.models.task_models import TaskRecord, WorkspaceState
from litehive.state.store import runtime_store

from litehive.state.locking import workspace_mutation_guard
from litehive.tasks.paths import task_dir, task_file
from litehive.tasks.persistence import (
    load_state,
    serialize_state,
    write_atomic_files_and_then,
)


def workspace_transition_writes(
    root: Path,
    *,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...] = (),
    state: WorkspaceState | None = None,
    journal_messages: dict[str, str] | None = None,
) -> dict[Path, str]:
    from litehive.state.records import serialize_task_record

    writes: dict[Path, str] = {}
    for task in tasks:
        writes[task_file(root, task)] = serialize_task_record(task)
        if journal_messages is None or task.id not in journal_messages:
            continue
        journal_path = task_dir(root, task) / "journal.md"
        existing = journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
        writes[journal_path] = f"{existing}\n## {utcnow()}\n{journal_messages[task.id]}\n"
    if state is not None:
        state = merged_state_for_runner_owned_write(
            root,
            state=state,
            protected_task_ids=[task.id for task in tasks],
        )
        writes[state_path(root)] = serialize_state(state)
    return writes


def _merge_queue_preserving_future_changes(
    *,
    desired_queue: list[str],
    latest_queue: list[str],
    protected_task_ids: list[str] | tuple[str, ...],
) -> list[str]:
    protected: list[str] = []
    seen_protected: set[str] = set()
    for task_id in protected_task_ids:
        if task_id in seen_protected:
            continue
        seen_protected.add(task_id)
        protected.append(task_id)
    if not protected:
        return list(desired_queue)

    protected_set = set(protected)
    latest_unprotected = [task_id for task_id in latest_queue if task_id not in protected_set]
    protected_positions = [
        (
            sum(1 for preceding in desired_queue[:index] if preceding not in protected_set),
            task_id,
        )
        for index, task_id in enumerate(desired_queue)
        if task_id in protected_set
    ]
    if not protected_positions:
        return list(latest_unprotected)

    merged = list(latest_unprotected)
    inserted = 0
    inserted_ids: set[str] = set()
    for unprotected_before, task_id in protected_positions:
        if task_id in inserted_ids:
            continue
        insertion_index = min(unprotected_before + inserted, len(merged))
        merged.insert(insertion_index, task_id)
        inserted_ids.add(task_id)
        inserted += 1
    return merged


def merged_state_for_runner_owned_write(
    root: Path,
    *,
    state: WorkspaceState,
    protected_task_ids: list[str] | tuple[str, ...] = (),
) -> WorkspaceState:
    latest_state = load_state(root)
    merged_state = state.model_copy(deep=True)
    merged_state.queue = _merge_queue_preserving_future_changes(
        desired_queue=state.queue,
        latest_queue=latest_state.queue,
        protected_task_ids=protected_task_ids,
    )
    merged_state.next_task_number = max(state.next_task_number, latest_state.next_task_number)
    return merged_state


def persist_task_and_state(
    root: Path,
    *,
    task: TaskRecord,
    state: WorkspaceState,
    journal_message: str | None = None,
) -> None:
    persist_tasks_and_state(
        root,
        tasks=[task],
        state=state,
        journal_messages={task.id: journal_message} if journal_message is not None else None,
    )


def persist_tasks_and_state(
    root: Path,
    *,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...],
    state: WorkspaceState,
    journal_messages: dict[str, str] | None = None,
) -> None:
    from litehive.state.records import ensure_runtime_ignored, task_state_for_storage

    for task in tasks:
        task.updated_at = utcnow()
    writes = workspace_transition_writes(
        root,
        tasks=tasks,
        state=state,
        journal_messages=journal_messages,
    )
    with workspace_mutation_guard(root):
        merged_state = merged_state_for_runner_owned_write(
            root,
            state=state,
            protected_task_ids=[task.id for task in tasks],
        )
        write_atomic_files_and_then(
            writes,
            lambda: runtime_store(root).save_runtime_transaction(
                task_states={task.id: task_state_for_storage(task) for task in tasks},
                workspace_state=merged_state,
            ),
        )
        ensure_runtime_ignored(root)


def persist_tasks_and_state_without_runner_guard(
    root: Path,
    *,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...],
    state: WorkspaceState,
    journal_messages: dict[str, str] | None = None,
) -> None:
    from litehive.state.records import ensure_runtime_ignored, task_state_for_storage

    for task in tasks:
        task.updated_at = utcnow()
    writes = workspace_transition_writes(
        root,
        tasks=tasks,
        state=state,
        journal_messages=journal_messages,
    )
    merged_state = merged_state_for_runner_owned_write(
        root,
        state=state,
        protected_task_ids=[task.id for task in tasks],
    )
    write_atomic_files_and_then(
        writes,
        lambda: runtime_store(root).save_runtime_transaction(
            task_states={task.id: task_state_for_storage(task) for task in tasks},
            workspace_state=merged_state,
        ),
    )
    ensure_runtime_ignored(root)


def persist_task_and_state_without_runner_guard(
    root: Path,
    *,
    task: TaskRecord,
    state: WorkspaceState,
    journal_message: str | None = None,
) -> None:
    persist_tasks_and_state_without_runner_guard(
        root,
        tasks=[task],
        state=state,
        journal_messages={task.id: journal_message} if journal_message is not None else None,
    )
