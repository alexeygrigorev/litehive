"""Workspace state persistence and atomic write helpers."""

import gzip
import os
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.state.store import runtime_store
from litehive.tasks.paths import task_dir, task_file

_MISSING = object()


def load_state(root: Path) -> WorkspaceState:
    ensure_workspace(root)
    store = runtime_store(root)
    state = store.load_workspace_state()
    if state is None:
        state = WorkspaceState()
        store.save_workspace_state(state)
    return state


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            if not os.environ.get("LITEHIVE_SKIP_FSYNC"):
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def atomic_write_gzip_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with gzip.open(temp_path, "wt", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_atomic_files(writes: dict[Path, str]) -> None:
    snapshots = {
        path: path.read_text(encoding="utf-8") if path.exists() else _MISSING for path in writes
    }
    applied: list[Path] = []
    try:
        for path, content in writes.items():
            atomic_write_text(path, content)
            applied.append(path)
    except Exception:
        for path in reversed(applied):
            previous = snapshots[path]
            if previous is _MISSING:
                if path.exists():
                    path.unlink()
                continue
            atomic_write_text(path, previous)
        raise


def write_atomic_files_and_then(writes: dict[Path, str], callback) -> None:
    snapshots = {
        path: path.read_text(encoding="utf-8") if path.exists() else _MISSING for path in writes
    }
    applied: list[Path] = []
    try:
        for path, content in writes.items():
            atomic_write_text(path, content)
            applied.append(path)
        callback()
    except Exception:
        for path in reversed(applied):
            previous = snapshots[path]
            if previous is _MISSING:
                if path.exists():
                    path.unlink()
                continue
            atomic_write_text(path, previous)
        raise


def save_state(root: Path, state: WorkspaceState) -> None:
    from litehive.state.locking import workspace_mutation_guard

    with workspace_mutation_guard(root):
        runtime_store(root).save_workspace_state(state)


def save_state_without_runner_guard(root: Path, state: WorkspaceState) -> None:
    runtime_store(root).save_workspace_state(state)


def set_pool_stop_reason(root: Path, stop_reason: str | None) -> WorkspaceState:
    from litehive.state.locking import workspace_lock

    with workspace_lock(root):
        state = load_state(root)
        state.pool_stop_reason = stop_reason
        save_state_without_runner_guard(root, state)
        return state


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
    protected_task_ids: list[str] | tuple[str, ...] = (),
) -> None:
    persist_tasks_and_state(
        root,
        tasks=[task],
        state=state,
        journal_messages={task.id: journal_message} if journal_message is not None else None,
        protected_task_ids=protected_task_ids,
    )


def persist_tasks_and_state(
    root: Path,
    *,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...],
    state: WorkspaceState,
    journal_messages: dict[str, str] | None = None,
    protected_task_ids: list[str] | tuple[str, ...] = (),
) -> None:
    from litehive.state.locking import workspace_mutation_guard
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
            protected_task_ids=[*protected_task_ids, *[task.id for task in tasks]],
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
    protected_task_ids: list[str] | tuple[str, ...] = (),
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
        protected_task_ids=[*protected_task_ids, *[task.id for task in tasks]],
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
    protected_task_ids: list[str] | tuple[str, ...] = (),
) -> None:
    persist_tasks_and_state_without_runner_guard(
        root,
        tasks=[task],
        state=state,
        journal_messages={task.id: journal_message} if journal_message is not None else None,
        protected_task_ids=protected_task_ids,
    )
