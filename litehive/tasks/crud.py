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
from litehive.git_ops import default_commit_message
from litehive.models import (
    FollowUpTaskSpec,
    GitHubOrigin,
    TaskCreationSource,
    TaskRecord,
    TaskRuntime,
    UpstreamContributionOrigin,
    utcnow,
)

from .constants import (
    VALID_PLANNED_EFFORTS,
    VALID_PM_COMPLEXITIES,
    VALID_TASK_PRIORITIES,
    VALID_TASK_TYPES,
)
from .locking import _workspace_lock, workspace_mutation_guard
from .normalization import normalize_acceptance_criteria, normalize_human_checkpoints
from .paths import slugify, task_dir, task_file, task_runtime_file, tasks_root
from .persistence import (
    _atomic_write_text,
    _serialize_state,
    _write_atomic_files,
    load_state,
)
from .templates import apply_task_template_defaults, render_task_brief, task_brief_file

logger = logging.getLogger(__name__)


def _ensure_runtime_ignored(root: Path) -> None:
    ignore_path = workspace_gitignore_path(root)
    expected = render_workspace_gitignore()
    if not ignore_path.exists() or ignore_path.read_text(encoding="utf-8") != expected:
        ignore_path.write_text(expected, encoding="utf-8")


def _serialize_task_record(task: TaskRecord) -> str:
    _normalize_task_worktree_state(task)
    payload = task.model_dump(mode="python")
    payload["git"]["worktree_path"] = None
    return yaml.safe_dump(payload, sort_keys=False)


def _serialize_task_runtime(task: TaskRecord) -> str:
    _normalize_task_worktree_state(task)
    return yaml.safe_dump(
        {
            **task.runtime.model_dump(mode="python"),
            "git": {
                "commit_sha": task.git.commit_sha,
                "worktree_path": task.runtime.git.worktree_path,
            },
        },
        sort_keys=False,
    )


def _write_task_runtime(root: Path, task: TaskRecord) -> None:
    _atomic_write_text(task_runtime_file(root, task), _serialize_task_runtime(task))
    _ensure_runtime_ignored(root)


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


def save_task_runtime(root: Path, task: TaskRecord) -> None:
    with workspace_mutation_guard(root):
        _write_task_runtime(root, task)


def _load_task_runtime(root: Path, task: TaskRecord) -> TaskRecord:
    runtime_file = task_runtime_file(root, task)
    if not runtime_file.exists():
        _normalize_task_worktree_state(task)
        return task
    data = yaml.safe_load(runtime_file.read_text(encoding="utf-8")) or {}
    task.runtime = TaskRuntime(**data)
    set_task_commit_sha(task, task.runtime.git.commit_sha)
    _normalize_task_worktree_state(task)
    return task


def _load_task_record_file(path: Path) -> TaskRecord:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return TaskRecord(**data)


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


def create_task(
    root: Path,
    *,
    title: str,
    depends_on: list[str] | None = None,
    mode: str = "implementation",
    pipeline_mode: str = "full",
    task_type: str | None = None,
    engine: str | None = None,
    model: str | None = None,
    retry_limit: int | None = None,
    goal: str = "",
    acceptance_criteria: list[str] | None = None,
    pm_complexity: str | None = None,
    planned_effort: str | None = None,
    human_checkpoints: list[str] | None = None,
    auto_commit: bool = True,
    upstream_origin: UpstreamContributionOrigin | None = None,
    github_origin: GitHubOrigin | None = None,
    priority: str | None = None,
) -> TaskRecord:
    from .queue_ops import _validate_task_dependencies

    ensure_workspace(root)
    if retry_limit is not None and retry_limit < 0:
        raise ValueError("Retry limit must be 0 or greater")
    if pipeline_mode not in {"single", "full"}:
        raise ValueError(f"Unsupported pipeline_mode '{pipeline_mode}'")
    if priority is not None and priority not in VALID_TASK_PRIORITIES:
        raise ValueError(f"Unsupported priority '{priority}'; choose from {sorted(VALID_TASK_PRIORITIES)}")
    if task_type is not None and task_type not in VALID_TASK_TYPES:
        raise ValueError(f"Unsupported task type '{task_type}'")
    if pm_complexity is not None and pm_complexity not in VALID_PM_COMPLEXITIES:
        raise ValueError(f"Unsupported PM complexity '{pm_complexity}'")
    if planned_effort is not None and planned_effort not in VALID_PLANNED_EFFORTS:
        raise ValueError(f"Unsupported planned effort '{planned_effort}'")
    if priority is not None and priority not in VALID_TASK_PRIORITIES:
        raise ValueError(f"Unsupported priority '{priority}'")
    with _workspace_lock(root):
        state = load_state(root)
        task_id = f"T-{_reserve_next_task_numbers(root, state)[0]:04d}"
        slug = slugify(title)
        _validate_task_dependencies(root, task_id=task_id, depends_on=depends_on or [])
        task = TaskRecord(
            id=task_id,
            slug=slug,
            title=title,
            depends_on=list(depends_on or []),
            task_type=task_type,
            engine=engine,
            model=model,
            mode=mode,  # type: ignore[arg-type]
            pipeline_mode=pipeline_mode,  # type: ignore[arg-type]
            priority=priority or "medium",
            goal=goal,
            acceptance_criteria=normalize_acceptance_criteria(acceptance_criteria),
            pm_complexity=pm_complexity,  # type: ignore[arg-type]
            planned_effort=planned_effort,  # type: ignore[arg-type]
            human_checkpoints=normalize_human_checkpoints(human_checkpoints),
            retry_policy={"max_retries": retry_limit},
            git={
                "auto_commit": auto_commit,
                "commit_message": default_commit_message(task_id, slug),
            },
            upstream_origin=upstream_origin,
            github_origin=github_origin,
        )
        task = apply_task_template_defaults(task)

        base = task_dir(root, task)
        (base / "reports").mkdir(parents=True, exist_ok=False)
        (base / "subagents").mkdir(parents=True, exist_ok=False)
        (base / "artifacts").mkdir(parents=True, exist_ok=False)
        state.queue.append(task.id)
        try:
            import sys
            _merged_state = sys.modules["litehive.tasks"]._merged_state_for_runner_owned_write

            state = _merged_state(
                root,
                state=state,
                protected_task_ids=[task.id],
            )
            writes = {
                task_file(root, task): yaml.safe_dump(
                    task.model_dump(mode="python"), sort_keys=False
                ),
                task_runtime_file(root, task): _serialize_task_runtime(task),
                base / "journal.md": f"# {task.id} {task.title}\n\n## {utcnow()}\nTask created.\n",
                state_path(root): _serialize_state(state),
            }
            if task.mode == "tasks":
                writes[task_brief_file(root, task)] = render_task_brief(task)
            _write_atomic_files(writes)
        except Exception:
            try:
                shutil.rmtree(base)
            except OSError as cleanup_err:
                logger.warning("Failed to clean up %s: %s", base, cleanup_err)
            raise
        _ensure_runtime_ignored(root)
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
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        reserved_numbers = _reserve_next_task_numbers(root, state, count=len(follow_ups))
        writes: dict[Path, str] = {}

        for next_number, follow_up in zip(reserved_numbers, follow_ups):
            task_id = f"T-{next_number:04d}"
            slug = slugify(follow_up.title)
            mode = "tasks" if follow_up.task_type else "implementation"
            task = TaskRecord(
                id=task_id,
                slug=slug,
                title=follow_up.title,
                mode=mode,  # type: ignore[arg-type]
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
            task = apply_task_template_defaults(task)

            base = task_dir(root, task)
            (base / "reports").mkdir(parents=True, exist_ok=False)
            (base / "subagents").mkdir(parents=True, exist_ok=False)
            (base / "artifacts").mkdir(parents=True, exist_ok=False)
            created_dirs.append(base)
            state.queue.append(task.id)
            writes[task_file(root, task)] = yaml.safe_dump(
                task.model_dump(mode="python"), sort_keys=False
            )
            writes[task_runtime_file(root, task)] = _serialize_task_runtime(task)
            writes[base / "journal.md"] = (
                f"# {task.id} {task.title}\n\n"
                f"## {utcnow()}\n"
                "Task created.\n\n"
                f"Created as a follow-up from `{parent_task.id}` during `{stage}`.\n"
                f"Rationale: {follow_up.rationale}\n"
            )
            if task.mode == "tasks":
                writes[task_brief_file(root, task)] = render_task_brief(task)
            created_tasks.append(task)

        import sys
        _merged_state = sys.modules["litehive.tasks"]._merged_state_for_runner_owned_write

        state = _merged_state(
            root,
            state=state,
            protected_task_ids=[task.id for task in created_tasks],
        )
        writes[state_path(root)] = _serialize_state(state)
        try:
            _write_atomic_files(writes)
        except Exception:
            for base in reversed(created_dirs):
                try:
                    shutil.rmtree(base)
                except OSError as cleanup_err:
                    logger.warning("Failed to clean up %s: %s", base, cleanup_err)
            raise
        _ensure_runtime_ignored(root)
    return created_tasks


def discard_created_task(root: Path, task_id: str) -> None:
    with _workspace_lock(root):
        task = get_task(root, task_id)
        state = load_state(root)
        if state.active_task_id == task_id:
            state.active_task_id = None
        state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
        from .persistence import _save_state_without_runner_guard

        _save_state_without_runner_guard(root, state)
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
        task = _load_task_record_file(path)
        if include_runtime:
            task = _load_task_runtime(root, task)
        records.append(task)
    return records


def list_tasks_state_first(
    root: Path,
    *,
    state=None,
    include_runtime: bool = False,
) -> list[TaskRecord]:
    task_by_id: dict[str, TaskRecord] = {}
    for child in sorted(tasks_root(root).iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if not path.exists():
            continue
        task = _load_task_record_file(path)
        if include_runtime:
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
    from .workflow import _workspace_transition_writes

    task.updated_at = utcnow()
    with workspace_mutation_guard(root):
        writes = _workspace_transition_writes(root, tasks=[task])
        _write_atomic_files(writes)
        _ensure_runtime_ignored(root)
