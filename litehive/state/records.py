"""Task CRUD operations: create, list, get, save, and related helpers."""

import logging
import os
import re
from pathlib import Path

import yaml

from litehive.config.workspace_files import workspace_gitignore_path
from litehive.config.workspace import ensure_workspace, render_workspace_gitignore
from litehive.git.ops import default_commit_message
from litehive.domain.common import utcnow
from litehive.domain.reports import FollowUpTaskSpec
from litehive.fs_cleanup import remove_tree_logged
from litehive.domain.task import (
    TaskCreationSource,
    TaskRecord,
    TaskIntentRecord,
    TaskStateRecord,
    WorkspaceState,
)
from litehive.state.store import runtime_store

from litehive.tasks.constants import (
    VALID_TASK_PRIORITIES,
    VALID_TASK_TYPES,
)
from litehive.state.locking import workspace_lock, workspace_mutation_guard
from litehive.state.persist import (
    load_state,
    save_state_without_runner_guard,
    write_atomic_files_and_then,
)
from litehive.tasks.audit import (
    TaskAuditEntry,
    append_task_audit_entries,
    build_task_audit_entry,
    snapshot_task_audit_state,
)
from litehive.tasks.normalization import normalize_acceptance_criteria
from litehive.tasks.paths import slugify, task_dir, tasks_root

logger = logging.getLogger(__name__)

_MANUAL_CREATION_RATIONALE = "Created outside a Litehive agent session."


class TaskStateMissingError(RuntimeError):
    """Raised when a task has no SQLite runtime state row."""


def _load_task_record_mapping(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Task file must contain a mapping: {path}")
    return dict(loaded)


def _highest_task_number_on_disk(root: Path) -> int:
    existing = []
    for child in tasks_root(root, bootstrap=False).iterdir():
        if not child.is_dir():
            continue
        match = re.match(r"^T-(\d{4})-", child.name)
        if match:
            existing.append(int(match.group(1)))
    return max(existing, default=0)


def _reserve_next_task_numbers(root, state, *, count: int = 1) -> list[int]:
    if count < 1:
        raise ValueError("count must be 1 or greater")
    if state.next_task_number <= 0:
        state.next_task_number = _highest_task_number_on_disk(root)
    start = state.next_task_number + 1
    state.next_task_number += count
    return list(range(start, start + count))


def _task_creation_stage(root: Path, *, current_task_id: str | None) -> str | None:
    env_stage = (os.environ.get("LITEHIVE_STAGE") or "").strip()
    if env_stage:
        return env_stage
    if not current_task_id:
        return None
    current_task = get_task_record(root, current_task_id)
    if current_task is None:
        return None
    runtime_stage = current_task.runtime.current_stage.stage
    if runtime_stage:
        return runtime_stage
    pipeline_stage = str(current_task.pipeline_status).strip()
    if pipeline_stage and pipeline_stage != "backlog":
        return pipeline_stage
    return None


def _default_task_creation_source(root: Path) -> TaskCreationSource:
    agent_role = (os.environ.get("LITEHIVE_AGENT_ROLE") or "").strip()
    current_task_id = (os.environ.get("LITEHIVE_TASK_ID") or "").strip() or None
    if not agent_role:
        return TaskCreationSource(
            source="manual",
            rationale=_MANUAL_CREATION_RATIONALE,
        )
    rationale = f"Created by Litehive agent role {agent_role}."
    if current_task_id:
        rationale = f"{rationale[:-1]} while working on {current_task_id}."
    return TaskCreationSource(
        source="agent",
        task_id=current_task_id,
        stage=_task_creation_stage(root, current_task_id=current_task_id),
        role=agent_role,
        rationale=rationale,
    )


def ensure_runtime_ignored(root: Path) -> None:
    ignore_path = workspace_gitignore_path(root)
    expected = render_workspace_gitignore()
    if not ignore_path.exists() or ignore_path.read_text(encoding="utf-8") != expected:
        ignore_path.write_text(expected, encoding="utf-8")


def serialize_task_record(task: TaskRecord) -> str:
    _normalize_task_commit_sha_state(task)
    _normalize_task_worktree_state(task)
    _normalize_task_flag_reason(task)
    payload = task.to_intent_record().model_dump(mode="json")
    return yaml.safe_dump(payload, sort_keys=False)


def task_state_for_storage(task: TaskRecord) -> TaskStateRecord:
    _normalize_task_commit_sha_state(task)
    _normalize_task_worktree_state(task)
    _normalize_task_flag_reason(task)
    return task.to_storage_state_record()


def write_task_runtime(root: Path, task: TaskRecord) -> None:
    runtime_store(root).save_task_state(task.id, task_state_for_storage(task))
    ensure_runtime_ignored(root)


def set_task_commit_sha(task: TaskRecord, commit_sha: str | None) -> None:
    task.git.commit_sha = commit_sha
    task.runtime.git.commit_sha = commit_sha


def get_task_worktree_path(task: TaskRecord) -> str | None:
    return task.runtime.git.worktree_path or task.git.worktree_path


def set_task_worktree_path(task: TaskRecord, worktree_path: str | None) -> None:
    task.runtime.git.worktree_path = worktree_path
    task.git.worktree_path = None


def clear_task_worktree_path(task: TaskRecord) -> None:
    set_task_worktree_path(task, None)


def _normalize_task_worktree_state(task: TaskRecord) -> None:
    if task.runtime.git.worktree_path:
        task.git.worktree_path = None
        return
    if task.git.worktree_path:
        set_task_worktree_path(task, task.git.worktree_path)


def _normalize_task_commit_sha_state(task: TaskRecord) -> None:
    if task.git.commit_sha:
        task.runtime.git.commit_sha = task.git.commit_sha
        return
    if task.runtime.git.commit_sha:
        task.git.commit_sha = task.runtime.git.commit_sha


def _normalize_task_flag_reason(task: TaskRecord) -> None:
    if task.status == "flagged":
        task.flag_reason = task.flag_reason or task.runtime.last_outcome.reason_code or "unknown"
        return
    if task.status == "deferred" and task.flag_count >= 3:
        return
    task.flag_reason = None


def _create_task_runtime_dirs(base: Path) -> None:
    (base / "reports").mkdir(parents=True, exist_ok=False)
    (base / "subagents").mkdir(parents=True, exist_ok=False)
    (base / "artifacts").mkdir(parents=True, exist_ok=False)


def _cleanup_created_task_dirs(paths: list[Path]) -> list[OSError]:
    errors: list[OSError] = []
    for path in reversed(paths):
        try:
            remove_tree_logged(path, logger=logger, target_label="created task directory")
        except OSError as cleanup_err:
            errors.append(cleanup_err)
    return errors


def _persist_created_tasks(
    root: Path,
    *,
    tasks: list[TaskRecord],
    state: WorkspaceState,
    writes: dict[Path, str],
    cleanup_dirs: list[Path],
    audit_entries: list[TaskAuditEntry] | None = None,
) -> None:
    from litehive.state.persist import merged_state_for_runner_owned_write, skip_bootstrap_load_state
    from litehive.tasks.duplicates import add_tasks_to_duplicate_index

    with skip_bootstrap_load_state():
        merged_state = merged_state_for_runner_owned_write(
            root,
            state=state,
            protected_task_ids=[task.id for task in tasks],
        )
    try:

        def callback() -> None:
            runtime_store(root).save_runtime_transaction(
                task_states={task.id: task_state_for_storage(task) for task in tasks},
                workspace_state=merged_state,
                audit_entries=audit_entries,
            )

        write_atomic_files_and_then(writes, callback)
        add_tasks_to_duplicate_index(root, tasks)
    except Exception as exc:
        cleanup_errors = _cleanup_created_task_dirs(cleanup_dirs)
        if cleanup_errors:
            raise ExceptionGroup(
                "failed to persist created tasks and roll back created task directories",
                [exc, *cleanup_errors],
            ) from exc
        raise


def save_task_runtime(root: Path, task: TaskRecord) -> None:
    with workspace_mutation_guard(root):
        write_task_runtime(root, task)


def _load_task_runtime(root: Path, task: TaskRecord) -> TaskRecord:
    store = runtime_store(root)
    task_state = store.load_task_state(task.id)
    if task_state is None:
        raise TaskStateMissingError(f"Task {task.id} is missing its SQLite runtime state row")
    task = TaskRecord.from_intent_and_state(task.to_intent_record(), task_state)
    _normalize_task_commit_sha_state(task)
    _normalize_task_worktree_state(task)
    return task


def load_task_record_file(path: Path) -> TaskRecord:
    data = _load_task_record_mapping(path)
    intent = TaskIntentRecord.model_validate(data)
    return TaskRecord.from_intent_and_state(intent)


def create_task(
    root: Path,
    *,
    title: str,
    depends_on: list[str] | None = None,
    pipeline_mode: str = "full",
    task_type: str | None = None,
    model: str | None = None,
    retry_limit: int | None = None,
    goal: str = "",
    acceptance_criteria: list[str] | None = None,
    auto_commit: bool = True,
    priority: str | None = None,
) -> TaskRecord:
    ensure_workspace(root)
    if retry_limit is not None and retry_limit < 0:
        raise ValueError("Retry limit must be 0 or greater")
    if pipeline_mode not in {"single", "full"}:
        raise ValueError(f"Unsupported pipeline_mode '{pipeline_mode}'")
    if priority is not None and priority not in VALID_TASK_PRIORITIES:
        raise ValueError(f"Unsupported priority '{priority}'; choose from {sorted(VALID_TASK_PRIORITIES)}")
    if task_type is not None and task_type not in VALID_TASK_TYPES:
        raise ValueError(f"Unsupported task type '{task_type}'")
    from litehive.tasks.queue import validate_task_dependencies

    with workspace_lock(root):
        state = load_state(root, bootstrap=False)
        task_id = f"T-{_reserve_next_task_numbers(root, state)[0]:04d}"
        slug = slugify(title)
        if depends_on:
            validate_task_dependencies(root, task_id=task_id, depends_on=depends_on)
        task = TaskRecord(
            id=task_id,
            slug=slug,
            title=title,
            depends_on=list(depends_on or []),
            task_type=task_type,
            model=model,
            pipeline_mode=pipeline_mode,  # type: ignore[arg-type]
            priority=priority or "medium",
            goal=goal,
            acceptance_criteria=normalize_acceptance_criteria(acceptance_criteria),
            retry_policy={"max_retries": retry_limit},
            created_from=_default_task_creation_source(root),
            git={
                "auto_commit": auto_commit,
                "commit_message": default_commit_message(task_id, slug),
            },
        )

        base = task_dir(root, task, bootstrap=False)
        _create_task_runtime_dirs(base)
        state.queue.append(task.id)
        writes = {
            base / "task.yaml": serialize_task_record(task),
            base / "journal.md": f"# {task.id} {task.title}\n\n## {utcnow()}\nTask created.\n",
        }
        actor = "operator"
        source = "manual"
        if task.created_from is not None and task.created_from.source == "agent":
            actor = "agent"
            source = "agent"
        elif task.created_from is not None and task.created_from.source == "follow_up":
            actor = "system"
            source = "follow_up"
        _persist_created_tasks(
            root,
            tasks=[task],
            state=state,
            writes=writes,
            cleanup_dirs=[base],
            audit_entries=[
                build_task_audit_entry(
                    task_id=task.id,
                    action="created",
                    actor=actor,
                    source=source,
                    after_task=task,
                    after_queue=state.queue,
                    context={
                        "title": task.title,
                        "priority": task.priority,
                        "pipeline_mode": str(task.pipeline_mode),
                        "created_from": (
                            None if task.created_from is None else task.created_from.model_dump(mode="json")
                        ),
                    },
                )
            ],
        )
        ensure_runtime_ignored(root)
        return task


def create_follow_up_tasks(
    root: Path,
    *,
    parent_task: TaskRecord,
    stage: str,
    follow_ups: list[FollowUpTaskSpec],
) -> list[TaskRecord]:
    if not follow_ups:
        return []
    if stage not in {"grooming", "testing", "accepting"}:
        return []

    ensure_workspace(root)
    created_tasks: list[TaskRecord] = []
    created_dirs: list[Path] = []
    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root, bootstrap=False)
        reserved_numbers = _reserve_next_task_numbers(root, state, count=len(follow_ups))
        writes: dict[Path, str] = {}

        for next_number, follow_up in zip(reserved_numbers, follow_ups):
            task_id = f"T-{next_number:04d}"
            slug = slugify(follow_up.title)
            task = TaskRecord(
                id=task_id,
                slug=slug,
                title=follow_up.title,
                task_type=follow_up.task_type,
                goal=follow_up.goal,
                acceptance_criteria=normalize_acceptance_criteria(follow_up.acceptance_criteria),
                created_from=TaskCreationSource(
                    source="follow_up",
                    task_id=parent_task.id,
                    stage=stage,  # type: ignore[arg-type]
                    rationale=follow_up.rationale,
                    blocking=follow_up.blocking,
                ),
                git={
                    "auto_commit": True,
                    "commit_message": default_commit_message(task_id, slug),
                },
            )

            base = task_dir(root, task, bootstrap=False)
            _create_task_runtime_dirs(base)
            created_dirs.append(base)
            state.queue.append(task.id)
            writes[base / "task.yaml"] = serialize_task_record(task)
            writes[base / "journal.md"] = (
                f"# {task.id} {task.title}\n\n"
                f"## {utcnow()}\n"
                "Task created.\n\n"
                f"Created as a follow-up from `{parent_task.id}` during `{stage}`.\n"
                f"Rationale: {follow_up.rationale}\n"
            )
            created_tasks.append(task)

        _persist_created_tasks(
            root,
            tasks=created_tasks,
            state=state,
            writes=writes,
            cleanup_dirs=created_dirs,
            audit_entries=[
                build_task_audit_entry(
                    task_id=task.id,
                    action="created",
                    actor="system",
                    source="follow_up",
                    after_task=task,
                    after_queue=state.queue,
                    context={
                        "title": task.title,
                        "priority": task.priority,
                        "pipeline_mode": str(task.pipeline_mode),
                        "created_from": (
                            None if task.created_from is None else task.created_from.model_dump(mode="json")
                        ),
                    },
                )
                for task in created_tasks
            ],
        )
        ensure_runtime_ignored(root)
    return created_tasks


def discard_created_task(root: Path, task_id: str) -> None:
    from litehive.tasks.duplicates import refresh_duplicate_task_index_if_initialized

    with workspace_lock(root):
        task = get_task(root, task_id)
        state = load_state(root)
        queue_before = list(state.queue)
        before_task = snapshot_task_audit_state(task)
        if state.active_task_id == task_id:
            state.active_task_id = None
        state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
        save_state_without_runner_guard(root, state)
        if task is not None:
            td = task_dir(root, task)
            if td.exists():
                remove_tree_logged(td, logger=logger, target_label="task directory")
        append_task_audit_entries(
            root,
            [
                build_task_audit_entry(
                    task_id=task_id,
                    action="removed",
                    actor="system",
                    source="task_cleanup",
                    before_task=before_task,
                    after_task=None,
                    before_queue=queue_before,
                    after_queue=state.queue,
                    context={"task_missing": task is None},
                )
            ],
        )
        refresh_duplicate_task_index_if_initialized(root)


def _task_record_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for child in sorted(tasks_root(root).iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if path.exists():
            paths.append(path)
    return paths


def _load_tasks_from_disk(
    root: Path,
    *,
    include_runtime: bool,
    strict: bool,
) -> list[TaskRecord]:
    records: list[TaskRecord] = []
    for path in _task_record_paths(root):
        try:
            task = load_task_record_file(path)
            if include_runtime:
                task = _load_task_runtime(root, task)
        except (TaskStateMissingError, ValueError):
            if strict:
                raise
            continue
        records.append(task)
    return records


def list_tasks(
    root: Path,
    *,
    include_runtime: bool = True,
    strict: bool = True,
) -> list[TaskRecord]:
    return _load_tasks_from_disk(root, include_runtime=include_runtime, strict=strict)


def list_tasks_state_first(
    root: Path,
    *,
    state: WorkspaceState | None = None,
    include_runtime: bool = False,
) -> list[TaskRecord]:
    task_by_id = {task.id: task for task in _load_tasks_from_disk(root, include_runtime=include_runtime, strict=True)}

    workspace_state = load_state(root) if state is None else state
    ordered_ids: list[str] = []
    seen: set[str] = set()

    def add(task_id: str | None) -> None:
        if task_id is None or task_id in seen or task_id not in task_by_id:
            return
        seen.add(task_id)
        ordered_ids.append(task_id)

    add(workspace_state.active_task_id)
    for task_id in workspace_state.queue:
        add(task_id)
    for task_id in sorted(task_by_id):
        add(task_id)

    return [task_by_id[task_id] for task_id in ordered_ids]


def get_task(root: Path, task_id: str) -> TaskRecord | None:
    for path in _task_record_paths(root):
        task = load_task_record_file(path)
        if task.id != task_id:
            continue
        return _load_task_runtime(root, task)
    return None


def get_task_record(root: Path, task_id: str) -> TaskRecord | None:
    """Return the task record, tolerating missing runtime rows."""
    for path in _task_record_paths(root):
        task = load_task_record_file(path)
        if task.id != task_id:
            continue
        try:
            return _load_task_runtime(root, task)
        except TaskStateMissingError:
            return task
    return None


def require_task(root: Path, task_id: str) -> TaskRecord:
    task = get_task(root, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    return task


def save_task(root: Path, task: TaskRecord) -> None:
    from litehive.state.persist import workspace_transition_writes
    from litehive.tasks.duplicates import refresh_duplicate_task_index_if_initialized

    task.updated_at = utcnow()
    with workspace_mutation_guard(root):
        writes = workspace_transition_writes(root, tasks=[task])
        write_atomic_files_and_then(
            writes,
            lambda: runtime_store(root).save_runtime_transaction(task_states={task.id: task_state_for_storage(task)}),
        )
        ensure_runtime_ignored(root)
        refresh_duplicate_task_index_if_initialized(root)
