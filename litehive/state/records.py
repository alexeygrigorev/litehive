"""Task CRUD operations: create, list, get, save, and related helpers."""

import logging
import os
from pathlib import Path

from litehive.config.workspace_files import workspace_gitignore_path
from litehive.config.workspace import ensure_workspace, render_workspace_gitignore
from litehive.git.ops import default_commit_message
from litehive.domain.common import PipelineMode, PipelineStatus, TaskStage, TaskStatus, utcnow
from litehive.domain.reports import FollowUpTaskSpec
from litehive.fs_cleanup import remove_tree_logged
from litehive.domain.task import (
    TaskCreationSource,
    TaskRecord,
    TaskStateRecord,
    WorkspaceState,
    canonicalize_task_terminal_state,
)
from litehive.state.store import runtime_store

from litehive.tasks.constants import VALID_TASK_PRIORITIES
from litehive.state.locking import workspace_lock, workspace_mutation_guard
from litehive.state.persist import (
    load_state,
    save_state_without_runner_guard,
    workspace_transition_writes,
    write_atomic_files_and_then,
)
from litehive.tasks.audit import (
    TaskAuditEntry,
    build_task_audit_entry,
    snapshot_task_audit_state,
)
from litehive.tasks.normalization import normalize_acceptance_criteria
from litehive.tasks.paths import slugify, task_dir

logger = logging.getLogger(__name__)

_MANUAL_CREATION_RATIONALE = "Created outside a Litehive agent session."


class TaskStateMissingError(RuntimeError):
    """Raised when a task has no SQLite runtime state row."""


def _highest_task_number_in_store(root: Path) -> int:
    return runtime_store(root).highest_task_number()


def _reserve_next_task_numbers(root, state, *, count: int = 1) -> list[int]:
    if count < 1:
        raise ValueError("count must be 1 or greater")
    if state.next_task_number <= 0:
        state.next_task_number = _highest_task_number_in_store(root)
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
    runtime_stage = current_task.runtime.pipeline.current_stage.stage
    if runtime_stage:
        return runtime_stage
    pipeline_stage = str(current_task.pipeline_status).strip()
    if pipeline_stage and pipeline_stage != PipelineStatus.BACKLOG:
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
    task.runtime.pipeline.git.commit_sha = commit_sha


def get_task_worktree_path(task: TaskRecord) -> str | None:
    return task.runtime.pipeline.git.worktree_path or task.git.worktree_path


def set_task_worktree_path(task: TaskRecord, worktree_path: str | None) -> None:
    task.runtime.pipeline.git.worktree_path = worktree_path
    task.git.worktree_path = None


def clear_task_worktree_path(task: TaskRecord) -> None:
    set_task_worktree_path(task, None)


def _normalize_task_worktree_state(task: TaskRecord) -> None:
    if task.runtime.pipeline.git.worktree_path:
        task.git.worktree_path = None
        return
    if task.git.worktree_path:
        set_task_worktree_path(task, task.git.worktree_path)


def _normalize_task_commit_sha_state(task: TaskRecord) -> None:
    if task.git.commit_sha:
        task.runtime.pipeline.git.commit_sha = task.git.commit_sha
        return
    if task.runtime.pipeline.git.commit_sha:
        task.git.commit_sha = task.runtime.pipeline.git.commit_sha


def _normalize_task_flag_reason(task: TaskRecord) -> None:
    canonicalize_task_terminal_state(task)
    if task.status == TaskStatus.FLAGGED:
        task.flag_reason = task.flag_reason or task.runtime.pipeline.last_outcome.reason_code or "unknown"
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
    task_journal_messages: dict[str, str] | None = None,
    cleanup_dirs: list[Path],
    audit_entries: list[TaskAuditEntry] | None = None,
) -> None:
    # inline: kept so tests can monkey-patch ``merged_state_for_runner_owned_write``
    # on the persist module (the canonical home) and have callers here see it.
    from litehive.state.persist import merged_state_for_runner_owned_write, skip_bootstrap_load_state  # noqa: PLC0415

    with skip_bootstrap_load_state():
        merged_state = merged_state_for_runner_owned_write(
            root,
            state=state,
            protected_task_ids=[task.id for task in tasks],
        )
    try:

        def callback() -> None:
            runtime_store(root).save_runtime_transaction(
                task_intents={task.id: task.to_intent_record() for task in tasks},
                task_states={task.id: task_state_for_storage(task) for task in tasks},
                workspace_state=merged_state,
                task_journal_messages=task_journal_messages,
                audit_entries=audit_entries,
            )

        write_atomic_files_and_then(writes, callback)
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


def create_task(
    root: Path,
    *,
    title: str,
    depends_on: list[str] | None = None,
    pipeline_mode: str = "full",
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
    try:
        pipeline_mode_enum = PipelineMode(pipeline_mode)
    except ValueError:
        raise ValueError(f"Unsupported pipeline_mode '{pipeline_mode}'") from None
    if priority is not None and priority not in VALID_TASK_PRIORITIES:
        raise ValueError(f"Unsupported priority '{priority}'; choose from {sorted(VALID_TASK_PRIORITIES)}")
    # inline: tasks.queue top-level-imports state.records (would cycle).
    from litehive.tasks.queue import validate_task_dependencies  # noqa: PLC0415

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
            model=model,
            pipeline_mode=pipeline_mode_enum,
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
        writes: dict[Path, str] = {}
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
            task_journal_messages={task.id: "Task created."},
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
    if stage not in {TaskStage.GROOMING, TaskStage.TESTING, TaskStage.ACCEPTING}:
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
                goal=follow_up.goal,
                acceptance_criteria=normalize_acceptance_criteria(follow_up.acceptance_criteria),
                created_from=TaskCreationSource(
                    source="follow_up",
                    task_id=parent_task.id,
                    stage=stage,
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
            created_tasks.append(task)

        _persist_created_tasks(
            root,
            tasks=created_tasks,
            state=state,
            writes=writes,
            task_journal_messages={
                task.id: (
                    "Task created.\n\n"
                    f"Created as a follow-up from `{parent_task.id}` during `{stage}`.\n"
                    f"Rationale: {follow_up.rationale}"
                )
                for task, follow_up in zip(created_tasks, follow_ups)
            },
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
        runtime_store(root).delete_task_records(
            task_id,
            audit_entries=[
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


def _load_tasks_from_store(
    root: Path,
    *,
    include_runtime: bool,
    strict: bool,
) -> list[TaskRecord]:
    store = runtime_store(root)
    records: list[TaskRecord] = []
    for intent in store.list_task_intents():
        try:
            state_record = store.load_task_state(intent.id)
            stateful_task = TaskRecord.from_intent_and_state(intent, state_record)
            if include_runtime:
                if state_record is None:
                    raise TaskStateMissingError(f"Task {intent.id} is missing its SQLite runtime state row")
                task = stateful_task
            else:
                task = TaskRecord.from_intent_and_state(intent)
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
    return _load_tasks_from_store(
        root,
        include_runtime=include_runtime,
        strict=strict,
    )


def list_tasks_state_first(
    root: Path,
    *,
    state: WorkspaceState | None = None,
    include_runtime: bool = False,
) -> list[TaskRecord]:
    task_by_id = {task.id: task for task in _load_tasks_from_store(root, include_runtime=include_runtime, strict=True)}

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
    intent = runtime_store(root).load_task_intent(task_id)
    if intent is None:
        return None
    task = _load_task_runtime(root, TaskRecord.from_intent_and_state(intent))
    return task


def get_task_record(root: Path, task_id: str) -> TaskRecord | None:
    """Return the task record, tolerating missing runtime rows."""
    intent = runtime_store(root).load_task_intent(task_id)
    if intent is None:
        return None
    task = TaskRecord.from_intent_and_state(intent)
    try:
        task = _load_task_runtime(root, task)
    except TaskStateMissingError:
        pass
    return task


def require_task(root: Path, task_id: str) -> TaskRecord:
    task = get_task(root, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    return task


def save_task(root: Path, task: TaskRecord) -> None:
    task.updated_at = utcnow()
    with workspace_mutation_guard(root):
        writes = workspace_transition_writes(root, tasks=[task])
        write_atomic_files_and_then(
            writes,
            lambda: runtime_store(root).save_runtime_transaction(
                task_intents={task.id: task.to_intent_record()},
                task_states={task.id: task_state_for_storage(task)},
            ),
        )
        ensure_runtime_ignored(root)
