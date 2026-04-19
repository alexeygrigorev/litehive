"""Deterministic launch-recovery helpers for daemon and run-command entry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import yaml

from litehive.config.loading import load_config
from litehive.config.paths import litehive_root, workspace_path
from litehive.domain.reports import RecoveryAction, TaskActivityEntry
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.task import TaskRecord
from litehive.git.ops import GitError, is_git_repo, remove_worktree
from litehive.lifecycle.nodes.system import GitWorktreeSyncNode
from litehive.state.locking import (
    clear_runner_lock_metadata,
    read_runner_lock_metadata,
    runner_lock_is_held,
    runner_lock_pid_is_stale,
)
from litehive.state.persist import load_state
from litehive.state.records import (
    clear_task_worktree_path,
    get_task_worktree_path,
    load_task_record_file,
    save_task,
)
from litehive.state.store import runtime_store
from litehive.tasks.activity import append_task_activity
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.paths import tasks_root
from litehive.tasks.reports import record_recovery_report
from litehive.tasks.runtime import apply_task_outcome, finish_task_run_transition
from litehive.tasks.worktrees import (
    ensure_worktree_venv_link,
    resolve_recorded_worktree_path,
    serialize_worktree_path,
    task_worktree_branch,
    task_worktree_path,
)

from .workspace_repair import repair_workspace_state

LaunchFailureContext = Literal[
    "cycle_start_failed",
    "worktree_setup_failed",
    "venv_sync_failed",
    "pre_stage_setup_failed",
]


@dataclass(slots=True)
class LaunchFailure:
    context: LaunchFailureContext
    summary: str
    diagnostics: dict[str, str | int | bool | None | list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class LaunchRecoveryResult:
    fixed: bool
    summary: str
    actions: list[RecoveryAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocker: str | None = None


class TaskLaunchFailure(RuntimeError):
    """Raised when a task cannot be prepared for pipeline entry."""

    def __init__(
        self,
        *,
        context: LaunchFailureContext,
        summary: str,
        diagnostics: dict[str, str | int | bool | None | list[str]] | None = None,
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
        corrupt = corrupt_tasks.get(preferred_task_id)
        if corrupt is not None:
            return corrupt.task
        task = next((candidate for candidate in tasks if candidate.id == preferred_task_id), None)
        if task is not None:
            return task

    ordered_tasks = [task for task in [*tasks, *[candidate.task for candidate in corrupt_tasks.values()]] if task.status != "flagged"]
    if not ordered_tasks:
        ordered_tasks = [*tasks, *[candidate.task for candidate in corrupt_tasks.values()]]
    task_ids = {task.id: task for task in ordered_tasks}

    try:
        state = load_state(root, bootstrap=False)
    except Exception:
        return ordered_tasks[0]

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


def _load_task_records_without_bootstrap(root: Path) -> tuple[list[TaskRecord], dict[str, CorruptTaskCandidate]]:
    records: list[TaskRecord] = []
    corrupt_tasks: dict[str, CorruptTaskCandidate] = {}
    store = runtime_store(root)
    tasks_dir = tasks_root(root, bootstrap=False)
    if not tasks_dir.exists():
        return records, corrupt_tasks
    for child in sorted(tasks_dir.iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if not path.exists():
            continue
        try:
            task = load_task_record_file(path)
        except Exception as exc:
            candidate = _corrupt_task_candidate(store, child, path, exc)
            if candidate is not None:
                corrupt_tasks[candidate.task.id] = candidate
            continue
        state = store.load_task_state(task.id)
        if state is not None:
            task = state.apply_to_task(task)
        records.append(task)
    return records, corrupt_tasks


def _corrupt_task_candidate(store, task_dir: Path, task_yaml_path: Path, exc: Exception) -> CorruptTaskCandidate | None:
    match = _TASK_DIR_RE.match(task_dir.name)
    if match is None:
        return None
    task_id, slug = match.groups()
    task = TaskRecord(
        id=task_id,
        slug=slug,
        title=slug.replace("-", " ") or task_id,
    )
    state = store.load_task_state(task_id)
    if state is not None:
        task = state.apply_to_task(task)
    return CorruptTaskCandidate(
        task=task,
        path=task_yaml_path,
        error=f"{type(exc).__name__}: {exc}",
    )


def detect_cycle_start_failure(root: Path) -> LaunchFailure | None:
    path = litehive_root() / "workspaces.yaml"
    if not path.exists():
        return None
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return LaunchFailure(
            context="cycle_start_failed",
            summary=f"workspace registry is corrupt at {path} ({_yaml_location(exc)})",
            diagnostics={"path": str(path)},
        )
    except OSError as exc:
        return LaunchFailure(
            context="cycle_start_failed",
            summary=f"workspace registry is unreadable at {path}: {exc}",
            diagnostics={"path": str(path)},
        )
    return None


def prepare_task_launch(root: Path, task: TaskRecord) -> None:
    worktree = _ensure_task_worktree(root, task)
    _ensure_task_venv(root, task, worktree)
    _run_pre_stage_checks(root)


def attempt_launch_recovery(root: Path, task: TaskRecord, failure: LaunchFailure) -> LaunchRecoveryResult:
    actions: list[RecoveryAction] = []
    warnings: list[str] = []
    quarantine_requires_flag = False

    if failure.context in {"cycle_start_failed", "pre_stage_setup_failed"}:
        quarantine_requires_flag = _quarantine_corrupt_task_yaml(
            root,
            task,
            actions=actions,
            warnings=warnings,
        )
        _repair_legacy_registry(actions=actions, warnings=warnings)
        _repair_runner_lock(root, actions=actions, warnings=warnings)
        _repair_workspace_runtime(root, actions=actions, warnings=warnings)

    if failure.context == "worktree_setup_failed":
        _reset_task_worktree(root, task, actions=actions, warnings=warnings)

    if failure.context == "venv_sync_failed":
        _rebuild_task_venv(root, task, actions=actions, warnings=warnings)

    fixed = bool(actions) and not quarantine_requires_flag
    summary = (
        f"Launch recovery {failure.context} fixed: {failure.summary}"
        if fixed
        else f"Launch recovery {failure.context} failed: {failure.summary}"
    )
    blocker = None if fixed else failure.summary
    record_recovery_report(
        root,
        task,
        trigger_event_kind=TriggerEventKind.CRASH,
        origin_stage=_launch_origin_stage(task),
        summary=summary,
        runnable_state="runnable" if fixed else "blocked",
        failure_classification=failure.context,
        blocker=blocker,
        actions=actions,
        warnings=[
            *warnings,
            *[f"{key}: {value}" for key, value in sorted(failure.diagnostics.items()) if value not in (None, "", [], {})],
        ],
    )
    return LaunchRecoveryResult(
        fixed=fixed,
        summary=summary,
        actions=actions,
        warnings=warnings,
        blocker=blocker,
    )


def flag_task_after_failed_launch_recovery(root: Path, task: TaskRecord, failure: LaunchFailure) -> TaskRecord:
    reason = f"{failure.context}: {failure.summary}"
    origin_stage = _launch_origin_stage(task)
    task.status = "flagged"
    task.pipeline_status = "flagged"
    task.flag_reason = "recovery_failed"
    apply_task_outcome(
        task,
        kind="flagged",
        stage=origin_stage,
        reason_code="stage_exception",
        reason=reason,
        retry_count=task.runtime.retry_count,
        retry_limit=task.runtime.retry_limit,
        failure_classification=failure.context,
        failure_diagnostics=failure.diagnostics,
    )
    append_task_activity(
        root,
        task,
        TaskActivityEntry(
            role="recovery",
            stage=origin_stage,
            verdict="comment",
            message=f"Launch could not be recovered; task flagged.\ncontext: {failure.context}\nreason: {failure.summary}",
        ),
    )
    flagged = finish_task_run_transition(root, task, "flagged")
    try:
        from litehive.lifecycle.persistence import SqlitePersistence

        SqlitePersistence(root).reset(task.id)
    except Exception:
        pass
    return flagged


def _launch_origin_stage(task: TaskRecord) -> str:
    stage = task.pipeline_status
    if stage in {"backlog", "flagged", "", None}:
        return implementation_entry_stage(task)
    return stage


def _ensure_task_worktree(root: Path, task: TaskRecord) -> Path:
    recorded = resolve_recorded_worktree_path(root, get_task_worktree_path(task))
    if recorded is not None and recorded.exists():
        return recorded
    if not is_git_repo(root):
        return root

    branch = task_worktree_branch(task)
    existing = GitWorktreeSyncNode._registered_worktree_for_branch(root, branch)
    if existing is not None:
        task.runtime.git.worktree_path = serialize_worktree_path(existing)
        save_task(root, task)
        return existing

    worktree = task_worktree_path(root, task)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    try:
        GitWorktreeSyncNode._prune_stale_worktrees(root)
    except GitError as exc:
        raise TaskLaunchFailure(
            context="worktree_setup_failed",
            summary=str(exc),
            diagnostics={"branch": branch, "worktree_path": str(worktree)},
        ) from exc

    created = subprocess.run(
        ["git", "worktree", "add", "--force", "-B", branch, str(worktree), "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        raise TaskLaunchFailure(
            context="worktree_setup_failed",
            summary=created.stderr.strip() or created.stdout.strip() or "git worktree add failed",
            diagnostics={"branch": branch, "worktree_path": str(worktree)},
        )

    ensure_worktree_venv_link(root, worktree)
    task.runtime.git.worktree_path = serialize_worktree_path(worktree)
    save_task(root, task)
    return worktree


def _ensure_task_venv(root: Path, task: TaskRecord, worktree: Path) -> None:
    del root, task
    if not (worktree / "pyproject.toml").exists():
        return
    uv = shutil.which("uv")
    if uv is None:
        raise TaskLaunchFailure(
            context="venv_sync_failed",
            summary="uv executable is not available for task launch",
            diagnostics={"worktree_path": str(worktree)},
        )
    sync = subprocess.run(
        [uv, "sync", "--extra", "dev"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    if sync.returncode != 0:
        raise TaskLaunchFailure(
            context="venv_sync_failed",
            summary=f"uv sync failed in {worktree}",
            diagnostics={
                "worktree_path": str(worktree),
                "stderr": sync.stderr.strip() or sync.stdout.strip() or "uv sync failed",
            },
        )


def _run_pre_stage_checks(root: Path) -> None:
    try:
        load_config(root)
        load_state(root)
        read_runner_lock_metadata(root)
    except Exception as exc:
        raise TaskLaunchFailure(
            context="pre_stage_setup_failed",
            summary=f"pre-stage setup failed: {exc}",
        ) from exc


def _quarantine_corrupt_task_yaml(
    root: Path,
    task: TaskRecord,
    *,
    actions: list[RecoveryAction],
    warnings: list[str],
) -> bool:
    diagnostics = corrupt_task_launch_diagnostics(root, task.id)
    task_yaml_path = diagnostics.get("task_yaml_path")
    if not task_yaml_path:
        return False
    path = Path(task_yaml_path)
    if not path.exists():
        return True
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.corrupt-{timestamp}")
    try:
        path.replace(backup)
    except OSError as exc:
        warnings.append(f"failed to quarantine corrupt task.yaml {path}: {exc}")
        return False
    actions.append(
        RecoveryAction(
            action="quarantine_corrupt_task_yaml",
            summary=f"Moved corrupt task.yaml aside to {backup}.",
            metadata={
                "task_id": task.id,
                "path": str(path),
                "backup_path": str(backup),
                "error": diagnostics.get("task_yaml_error", ""),
            },
        )
    )
    return True


def _repair_legacy_registry(*, actions: list[RecoveryAction], warnings: list[str]) -> None:
    path = litehive_root() / "workspaces.yaml"
    if not path.exists():
        return
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
        return
    except yaml.YAMLError as exc:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.corrupt-{timestamp}")
        path.replace(backup)
        actions.append(
            RecoveryAction(
                action="quarantine_corrupt_workspaces_registry",
                summary=f"Moved corrupt workspace registry aside to {backup}.",
                metadata={"path": str(path), "backup_path": str(backup)},
            )
        )
        warnings.append(f"workspaces.yaml was corrupt ({_yaml_location(exc)})")
    except OSError as exc:
        warnings.append(f"failed to inspect workspaces.yaml: {exc}")


def _repair_runner_lock(root: Path, *, actions: list[RecoveryAction], warnings: list[str]) -> None:
    lock_path = workspace_path(root, "runtime", ".runner.lock")
    if not lock_path.exists():
        return

    try:
        yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        if runner_lock_is_held(root):
            warnings.append(f"runner lock is corrupt but still held: {exc}")
            return
        try:
            lock_path.unlink()
        except OSError as unlink_exc:
            warnings.append(f"failed to remove corrupt runner lock: {unlink_exc}")
            return
        actions.append(
            RecoveryAction(
                action="remove_corrupt_runner_lock",
                summary=f"Removed corrupt runner lock {lock_path}.",
                metadata={"path": str(lock_path)},
            )
        )
        return

    if runner_lock_pid_is_stale(root) and not runner_lock_is_held(root):
        clear_runner_lock_metadata(root)
        actions.append(
            RecoveryAction(
                action="clear_stale_runner_lock",
                summary=f"Cleared stale runner lock metadata at {lock_path}.",
                metadata={"path": str(lock_path)},
            )
        )


def _repair_workspace_runtime(root: Path, *, actions: list[RecoveryAction], warnings: list[str]) -> None:
    try:
        summary = repair_workspace_state(root, repair_broken_venvs_in_checkouts=True)
    except Exception as exc:
        warnings.append(f"workspace repair failed: {exc}")
        return

    if summary.cleared_active_task_id is not None or summary.requeued_task_ids:
        actions.append(
            RecoveryAction(
                action="repair_workspace_state",
                summary="Cleared stale runner/task state before relaunch.",
                metadata={
                    "cleared_active_task_id": summary.cleared_active_task_id,
                    "requeued_tasks": list(summary.requeued_task_ids),
                },
            )
        )
    if summary.broken_venv_binaries:
        warnings.append("broken venv binaries remain after workspace repair")


def _reset_task_worktree(root: Path, task: TaskRecord, *, actions: list[RecoveryAction], warnings: list[str]) -> None:
    worktree = resolve_recorded_worktree_path(root, get_task_worktree_path(task)) or task_worktree_path(root, task)
    branch = task_worktree_branch(task)

    if worktree.exists():
        try:
            remove_worktree(root, worktree, force=True)
        except GitError:
            try:
                shutil.rmtree(worktree)
            except OSError as exc:
                warnings.append(f"failed to remove stale worktree {worktree}: {exc}")
        else:
            actions.append(
                RecoveryAction(
                    action="remove_stale_worktree",
                    summary=f"Removed stale task worktree at {worktree}.",
                    metadata={"path": str(worktree)},
                )
            )

    clear_task_worktree_path(task)
    try:
        save_task(root, task)
    except Exception as exc:
        warnings.append(f"failed to clear recorded worktree path: {exc}")
    else:
        actions.append(
            RecoveryAction(
                action="clear_task_worktree_path",
                summary="Cleared the task's recorded worktree path so launch can recreate it.",
                metadata={"branch": branch},
            )
        )

    proc = subprocess.run(
        ["git", "worktree", "prune", "--expire", "now"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        actions.append(
            RecoveryAction(
                action="prune_git_worktrees",
                summary="Pruned stale git worktree metadata.",
            )
        )
    else:
        warnings.append(proc.stderr.strip() or proc.stdout.strip() or "git worktree prune failed")

    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def _rebuild_task_venv(root: Path, task: TaskRecord, *, actions: list[RecoveryAction], warnings: list[str]) -> None:
    worktree = resolve_recorded_worktree_path(root, get_task_worktree_path(task)) or task_worktree_path(root, task)
    if not worktree.exists() or not (worktree / "pyproject.toml").exists():
        warnings.append(f"task execution root is unavailable for venv rebuild: {worktree}")
        return

    uv = shutil.which("uv")
    if uv is None:
        warnings.append("uv executable not found while rebuilding task venv")
        return

    recreate = subprocess.run(
        [uv, "venv", "--clear", ".venv"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    if recreate.returncode != 0:
        warnings.append(recreate.stderr.strip() or recreate.stdout.strip() or "uv venv --clear failed")
        return

    sync = subprocess.run(
        [uv, "sync", "--extra", "dev"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    if sync.returncode != 0:
        warnings.append(sync.stderr.strip() or sync.stdout.strip() or "uv sync failed")
        return

    actions.append(
        RecoveryAction(
            action="rebuild_task_venv",
            summary=f"Recreated the task venv in {worktree} and ran `uv sync --extra dev`.",
            metadata={"path": str(worktree / '.venv')},
        )
    )


def _yaml_location(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return "line unknown"
    return f"line {mark.line + 1}"
