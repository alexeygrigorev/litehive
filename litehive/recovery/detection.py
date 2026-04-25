"""Failure detection and task selection helpers for recovery flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from litehive.config.registry import (
    legacy_workspace_registry_error,
    legacy_workspace_registry_path,
    workspace_registry_error,
    workspace_registry_path,
)
from litehive.domain.task import TaskRecord
from litehive.state.persist import load_state
from litehive.state.store import runtime_store

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
    del root, task_id
    return {}


def detect_cycle_start_failure(root: Path) -> LaunchFailure | None:
    del root
    legacy_path = legacy_workspace_registry_path()
    legacy_error = legacy_workspace_registry_error()
    if legacy_error is not None:
        return LaunchFailure(
            context="cycle_start_failed",
            summary=f"legacy workspace registry is corrupt at {legacy_path}: {legacy_error}",
            diagnostics={"path": str(legacy_path), "registry_kind": "legacy_yaml"},
        )
    path = workspace_registry_path()
    error = workspace_registry_error()
    if error is not None:
        return LaunchFailure(
            context="cycle_start_failed",
            summary=f"workspace registry database is corrupt at {path}: {error}",
            diagnostics={"path": str(path), "registry_kind": "sqlite_db"},
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
    for intent in store.list_task_intents():
        task = TaskRecord.from_intent_and_state(intent)
        state = store.load_task_state(task.id)
        if state is not None:
            task = state.apply_to_task(task)
        if task.status == "archived":
            continue
        records.append(task)
    return records, corrupt_tasks
