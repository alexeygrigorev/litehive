"""Task CRUD operations: create, list, get, save, and related helpers."""

import logging
import re
import shutil
from pathlib import Path

import yaml

from litehive.config import (
    ensure_workspace,
    render_workspace_gitignore,
    state_path,
    workspace_gitignore_path,
)
from litehive.git.ops import default_commit_message
from litehive.models import (
    FollowUpTaskSpec,
    TaskCreationSource,
    TaskRecord,
    TaskRuntime,
    TaskStateRecord,
    WorkspaceState,
    utcnow,
)
from litehive.storage import runtime_store

from .constants import (
    VALID_TASK_PRIORITIES,
    VALID_TASK_TYPES,
)
from litehive.workspace.locking import workspace_lock, workspace_mutation_guard
from .normalization import normalize_acceptance_criteria
from .paths import slugify, task_dir, task_file, task_runtime_file, tasks_root
from .persistence import (
    load_state,
    serialize_state,
    write_atomic_files_and_then,
)

logger = logging.getLogger(__name__)


def _drop_legacy_task_engine_field(data: object) -> dict:
    if not isinstance(data, dict):
        return {}
    sanitized = dict(data)
    sanitized.pop("engine", None)
    return sanitized


def _highest_task_number_on_disk(root: Path) -> int:
    existing = []
    for child in tasks_root(root).iterdir():
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


def ensure_runtime_ignored(root: Path) -> None:
    ignore_path = workspace_gitignore_path(root)
    expected = render_workspace_gitignore()
    if not ignore_path.exists() or ignore_path.read_text(encoding="utf-8") != expected:
        ignore_path.write_text(expected, encoding="utf-8")


def serialize_task_record(task: TaskRecord) -> str:
    _normalize_task_worktree_state(task)
    _normalize_task_flag_reason(task)
    payload = task.to_intent_record().model_dump(mode="python")
    return yaml.safe_dump(payload, sort_keys=False)


def serialize_task_runtime(task: TaskRecord) -> str:
    _normalize_task_worktree_state(task)
    return yaml.safe_dump(
        {
            **task_runtime_for_storage(task).model_dump(mode="python"),
            "git": {
                "commit_sha": task.git.commit_sha,
                "worktree_path": task.runtime.git.worktree_path,
            },
        },
        sort_keys=False,
    )


def task_runtime_for_storage(task: TaskRecord) -> TaskRuntime:
    _normalize_task_worktree_state(task)
    runtime = task.runtime.model_copy(deep=True)
    runtime.git.commit_sha = task.git.commit_sha
    runtime.git.worktree_path = task.runtime.git.worktree_path
    return runtime


def task_state_for_storage(task: TaskRecord) -> TaskStateRecord:
    _normalize_task_worktree_state(task)
    _normalize_task_flag_reason(task)
    state = task.to_state_record()
    state.runtime = task_runtime_for_storage(task)
    state.git.worktree_path = task.runtime.git.worktree_path
    state.updated_at = task.updated_at
    return state


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


def _normalize_task_flag_reason(task: TaskRecord) -> None:
    if task.status == "flagged":
        task.flag_reason = task.flag_reason or task.runtime.last_outcome.reason_code or "unknown"
        return
    if task.status == "deferred" and task.flag_count >= 3:
        return
    task.flag_reason = None


def save_task_runtime(root: Path, task: TaskRecord) -> None:
    with workspace_mutation_guard(root):
        write_task_runtime(root, task)


def _backfill_legacy_task_state(root: Path, task: TaskRecord) -> TaskRecord:
    store = runtime_store(root)
    writes = {task_file(root, task): serialize_task_record(task)}

    def callback() -> None:
        store.save_runtime_transaction(task_states={task.id: task_state_for_storage(task)})

    write_atomic_files_and_then(writes, callback)
    return task


def _load_task_runtime(root: Path, task: TaskRecord) -> TaskRecord:
    from .worktrees import migrate_legacy_worktree

    store = runtime_store(root)
    task_state = store.load_task_state(task.id)
    if task_state is not None:
        task = TaskRecord.from_intent_and_state(task.to_intent_record(), task_state)
        set_task_commit_sha(task, task.runtime.git.commit_sha)
        _normalize_task_worktree_state(task)
        _, changed = migrate_legacy_worktree(root, task)
        if changed:
            _backfill_legacy_task_state(root, task)
        return task

    runtime_file = task_runtime_file(root, task)
    if runtime_file.exists():
        data = yaml.safe_load(runtime_file.read_text(encoding="utf-8")) or {}
        task.runtime = TaskRuntime(**data)
        set_task_commit_sha(task, task.runtime.git.commit_sha)
    _normalize_task_worktree_state(task)
    _, changed = migrate_legacy_worktree(root, task)
    if changed or runtime_file.exists() or _task_file_contains_runtime_state(task_file(root, task)):
        return _backfill_legacy_task_state(root, task)
    return _backfill_legacy_task_state(root, task)


def _task_file_contains_runtime_state(path: Path) -> bool:
    data = _drop_legacy_task_engine_field(
        yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    )
    if not isinstance(data, dict):
        return False
    return any(
        key in data
        for key in (
            "model",
            "status",
            "flag_reason",
            "flag_count",
            "pipeline_status",
            "updated_at",
            "subagents",
            "retry_policy",
            "runtime",
        )
    ) or any(
        key in (data.get("git") or {})
        for key in (
            "commit_sha",
            "checkpoint_base_sha",
            "checkpoint_attempts",
            "rolled_back_checkpoint_attempt",
            "merge_agent_attempts",
            "worktree_path",
        )
    )


def load_task_record_file(path: Path) -> TaskRecord:
    data = _drop_legacy_task_engine_field(
        yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    )
    return TaskRecord(**data)


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
    from .queue_ops import validate_task_dependencies

    with workspace_lock(root):
        state = load_state(root)
        task_id = f"T-{_reserve_next_task_numbers(root, state)[0]:04d}"
        slug = slugify(title)
        validate_task_dependencies(root, task_id=task_id, depends_on=depends_on or [])
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
            git={
                "auto_commit": auto_commit,
                "commit_message": default_commit_message(task_id, slug),
            },
        )

        base = task_dir(root, task)
        (base / "reports").mkdir(parents=True, exist_ok=False)
        (base / "subagents").mkdir(parents=True, exist_ok=False)
        (base / "artifacts").mkdir(parents=True, exist_ok=False)
        state.queue.append(task.id)
        try:
            from litehive.workspace.workflow import merged_state_for_runner_owned_write

            state = merged_state_for_runner_owned_write(
                root,
                state=state,
                protected_task_ids=[task.id],
            )
            writes = {
                task_file(root, task): serialize_task_record(task),
                base / "journal.md": f"# {task.id} {task.title}\n\n## {utcnow()}\nTask created.\n",
                state_path(root): serialize_state(state),
            }

            def callback() -> None:
                runtime_store(root).save_runtime_transaction(
                    task_states={task.id: task_state_for_storage(task)},
                    workspace_state=state,
                )

            write_atomic_files_and_then(writes, callback)
        except Exception:
            try:
                shutil.rmtree(base)
            except OSError as cleanup_err:
                logger.warning("Failed to clean up %s: %s", base, cleanup_err)
            raise
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
    if stage not in {"grooming", "accepting"}:
        return []

    ensure_workspace(root)
    created_tasks: list[TaskRecord] = []
    created_dirs: list[Path] = []
    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root)
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

            base = task_dir(root, task)
            (base / "reports").mkdir(parents=True, exist_ok=False)
            (base / "subagents").mkdir(parents=True, exist_ok=False)
            (base / "artifacts").mkdir(parents=True, exist_ok=False)
            created_dirs.append(base)
            state.queue.append(task.id)
            writes[task_file(root, task)] = serialize_task_record(task)
            writes[base / "journal.md"] = (
                f"# {task.id} {task.title}\n\n"
                f"## {utcnow()}\n"
                "Task created.\n\n"
                f"Created as a follow-up from `{parent_task.id}` during `{stage}`.\n"
                f"Rationale: {follow_up.rationale}\n"
            )
            created_tasks.append(task)

        from litehive.workspace.workflow import merged_state_for_runner_owned_write

        state = merged_state_for_runner_owned_write(
            root,
            state=state,
            protected_task_ids=[task.id for task in created_tasks],
        )
        writes[state_path(root)] = serialize_state(state)
        try:
            def callback() -> None:
                runtime_store(root).save_runtime_transaction(
                    task_states={
                        task.id: task_state_for_storage(task)
                        for task in created_tasks
                    },
                    workspace_state=state,
                )

            write_atomic_files_and_then(writes, callback)
        except Exception:
            for base in reversed(created_dirs):
                try:
                    shutil.rmtree(base)
                except OSError as cleanup_err:
                    logger.warning("Failed to clean up %s: %s", base, cleanup_err)
            raise
        ensure_runtime_ignored(root)
    return created_tasks


def discard_created_task(root: Path, task_id: str) -> None:
    with workspace_lock(root):
        task = get_task(root, task_id)
        state = load_state(root)
        if state.active_task_id == task_id:
            state.active_task_id = None
        state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
        from .persistence import save_state_without_runner_guard

        save_state_without_runner_guard(root, state)
        if task is not None:
            td = task_dir(root, task)
            if td.exists():
                shutil.rmtree(td)


def list_tasks(root: Path, *, include_runtime: bool = True) -> list[TaskRecord]:
    records: list[TaskRecord] = []
    for child in sorted(tasks_root(root).iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if not path.exists():
            continue
        task = load_task_record_file(path)
        task = _load_task_runtime(root, task)
        records.append(task)
    return records


def list_tasks_state_first(
    root: Path,
    *,
    state: WorkspaceState | None = None,
    include_runtime: bool = False,
) -> list[TaskRecord]:
    task_by_id: dict[str, TaskRecord] = {}
    for child in sorted(tasks_root(root).iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if not path.exists():
            continue
        task = load_task_record_file(path)
        task = _load_task_runtime(root, task)
        task_by_id[task.id] = task

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
    for task in list_tasks(root):
        if task.id == task_id:
            return task
    return None


def require_task(root: Path, task_id: str) -> TaskRecord:
    task = get_task(root, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    return task


def save_task(root: Path, task: TaskRecord) -> None:
    from litehive.workspace.workflow import workspace_transition_writes

    task.updated_at = utcnow()
    with workspace_mutation_guard(root):
        writes = workspace_transition_writes(root, tasks=[task])
        write_atomic_files_and_then(
            writes,
            lambda: runtime_store(root).save_runtime_transaction(
                task_states={task.id: task_state_for_storage(task)}
            ),
        )
        ensure_runtime_ignored(root)
