"""Failure detection and task selection helpers for recovery flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Literal

from litehive.config.registry import workspace_registry_error, workspace_registry_path
from litehive.domain.task import TaskRecord
from litehive.state.persist import load_state
from litehive.state.records import load_task_record_file
from litehive.state.store import runtime_store
from litehive.tasks.paths import tasks_root

LaunchFailureContext = Literal[
    "cycle_start_failed",
    "worktree_setup_failed",
    "venv_sync_failed",
    "pre_stage_setup_failed",
]

LaunchDiagnostics = dict[str, str | int | bool | None | list[str]]


@dataclass(slots=True)
class LaunchFailure:
    context: LaunchFailureContext
    summary: str
    diagnostics: LaunchDiagnostics = field(default_factory=dict)


class TaskLaunchFailure(RuntimeError):
    """Raised when a task cannot be prepared for pipeline entry."""

    def __init__(
        self,
        *,
        context: LaunchFailureContext,
        summary: str,
        diagnostics: LaunchDiagnostics | None = None,
    ) -> None:
        super().__init__(summary)
        self.context = context
        self.summary = summary
        self.diagnostics = dict(diagnostics or {})

    def as_failure(self) -> LaunchFailure:
        return LaunchFailure(
            context=self.context,
            summary=self.summary,
            diagnostics=self.diagnostics,
        )


@dataclass(slots=True)
class CorruptTaskCandidate:
    task: TaskRecord
    path: Path
    error: str


_TASK_DIR_RE = re.compile(r"^(T-\d{4})-(.+)$")


def best_effort_recovery_task(root: Path, *, preferred_task_id: str | None = None) -> TaskRecord | None:
    try:
        tasks, corrupt_tasks = _load_task_records_without_bootstrap(root)
    except Exception:
        return None
    if not tasks and not corrupt_tasks:
        return None

    if preferred_task_id:
        preferred = corrupt_tasks.get(preferred_task_id)
        if preferred is not None:
            return preferred.task
        task = next((candidate for candidate in tasks if candidate.id == preferred_task_id), None)
        if task is not None:
            return task

    ordered_tasks = _ordered_recovery_candidates(tasks, corrupt_tasks)
    try:
        state = load_state(root, bootstrap=False)
    except Exception:
        return ordered_tasks[0]

    task_ids = {task.id: task for task in ordered_tasks}
    for candidate in (state.active_task_id, *state.queue):
        if candidate is None:
            continue
        task = task_ids.get(candidate)
        if task is not None:
            return task
    return ordered_tasks[0]


def corrupt_task_launch_diagnostics(root: Path, task_id: str | None) -> dict[str, str]:
    if not task_id:
        return {}
    try:
        _, corrupt_tasks = _load_task_records_without_bootstrap(root)
    except Exception:
        return {}
    corrupt = corrupt_tasks.get(task_id)
    if corrupt is None:
        return {}
    return {
        "task_yaml_path": str(corrupt.path),
        "task_yaml_error": corrupt.error,
    }


def detect_cycle_start_failure(root: Path) -> LaunchFailure | None:
    del root
    path = workspace_registry_path()
    error = workspace_registry_error()
    if error is not None:
        return LaunchFailure(
            context="cycle_start_failed",
            summary=f"workspace registry database is corrupt at {path}: {error}",
            diagnostics={"path": str(path)},
        )
    return None


def _ordered_recovery_candidates(
    tasks: list[TaskRecord],
    corrupt_tasks: dict[str, CorruptTaskCandidate],
) -> list[TaskRecord]:
    candidates = [*tasks, *[candidate.task for candidate in corrupt_tasks.values()]]
    ordered = [task for task in candidates if task.status != "flagged"]
    return ordered or candidates


def _load_task_records_without_bootstrap(root: Path) -> tuple[list[TaskRecord], dict[str, CorruptTaskCandidate]]:
    records: list[TaskRecord] = []
    corrupt_tasks: dict[str, CorruptTaskCandidate] = {}
    store = runtime_store(root)
    tasks_dir = tasks_root(root, bootstrap=False)
    if not tasks_dir.exists():
        return records, corrupt_tasks
    for child in sorted(tasks_dir.iterdir()):
        candidate = _load_task_record_candidate(store, child)
        if candidate is None:
            continue
        if isinstance(candidate, CorruptTaskCandidate):
            corrupt_tasks[candidate.task.id] = candidate
            continue
        records.append(candidate)
    return records, corrupt_tasks


def _load_task_record_candidate(store, task_dir: Path) -> TaskRecord | CorruptTaskCandidate | None:
    if not task_dir.is_dir():
        return None
    path = task_dir / "task.yaml"
    if not path.exists():
        return None
    try:
        task = load_task_record_file(path)
    except Exception as exc:
        return _corrupt_task_candidate(store, task_dir, path, exc)
    return _apply_runtime_state(store, task)


def _apply_runtime_state(store, task: TaskRecord) -> TaskRecord:
    state = store.load_task_state(task.id)
    if state is None:
        return task
    return state.apply_to_task(task)


def _corrupt_task_candidate(store, task_dir: Path, task_yaml_path: Path, exc: Exception) -> CorruptTaskCandidate | None:
    match = _TASK_DIR_RE.match(task_dir.name)
    if match is None:
        return None
    task_id, slug = match.groups()
    task = _apply_runtime_state(
        store,
        TaskRecord(
            id=task_id,
            slug=slug,
            title=slug.replace("-", " ") or task_id,
        ),
    )
    return CorruptTaskCandidate(
        task=task,
        path=task_yaml_path,
        error=f"{type(exc).__name__}: {exc}",
    )
