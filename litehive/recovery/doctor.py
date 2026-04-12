"""Workspace doctor checks and narrow automated fixes."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from litehive.config import ensure_workspace, state_path
from litehive.daemon.execution import check_origin_divergence
from litehive.models import TaskRecord, WorkspaceState
from litehive.recovery.detection import (
    is_orphaned_commit_stage_task,
    is_stranded_commit_task,
)
from litehive.tasks.constants import CLOSED_TASK_STATUSES
from litehive.recovery.workspace_repair import repair_workspace_state
from litehive.tasks.crud import list_tasks, save_task_runtime
from litehive.tasks.paths import tasks_root
from litehive.tasks.persistence import load_state, save_state_without_runner_guard
from litehive.tasks.queue_ops import is_task_eligible_for_execution
from litehive.tasks.worktrees import (
    is_managed_worktree_path,
    legacy_worktree_root,
    migrate_legacy_worktree,
)
from litehive.config import worktree_root


@dataclass(slots=True)
class DoctorFinding:
    code: str
    summary: str
    fix_command: str
    autofixable: bool = False


@dataclass(slots=True)
class DoctorReport:
    findings: list[DoctorFinding] = field(default_factory=list)
    state_error: str | None = None
    state_conflicted: bool = False
    stale_unmerged_worktrees_removed: int = 0


@dataclass(slots=True)
class DoctorFixResult:
    fixed: list[DoctorFinding] = field(default_factory=list)
    remaining: list[DoctorFinding] = field(default_factory=list)
    stale_unmerged_worktrees_removed: int = 0


def _state_edit_command() -> str:
    return "cp .litehive/state.yaml .litehive/state.yaml.bak && ${EDITOR:-vi} .litehive/state.yaml"


_TERMINAL_UNMERGED_WORKTREE_TASK_STATUSES = {"done", "abandoned", *CLOSED_TASK_STATUSES}


def _resolve_unmerged_worktree_path(root: Path, worktree_path: str) -> Path:
    candidate = Path(worktree_path)
    return candidate if candidate.is_absolute() else root / candidate


def _raw_task_status(root: Path, task_id: str) -> str | None:
    for path in sorted(tasks_root(root).glob(f"{task_id}-*/task.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(payload, dict):
            continue
        status = payload.get("status")
        if isinstance(status, str) and status.strip():
            return status.strip().lower()
    return None


def _prune_stale_unmerged_worktrees(root: Path, state: WorkspaceState) -> int:
    if not state.unmerged_worktrees:
        return 0

    task_statuses = {task.id: task.status for task in list_tasks(root)}
    kept = []
    removed = 0
    for entry in state.unmerged_worktrees:
        task_status = task_statuses.get(entry.task_id) or _raw_task_status(root, entry.task_id)
        missing_worktree = not _resolve_unmerged_worktree_path(root, entry.worktree_path).exists()
        if task_status in _TERMINAL_UNMERGED_WORKTREE_TASK_STATUSES or missing_worktree:
            removed += 1
            continue
        kept.append(entry)

    if removed:
        state.unmerged_worktrees = kept
        save_state_without_runner_guard(root, state)
    return removed


def _duplicate_id_findings(tasks: list[TaskRecord]) -> list[DoctorFinding]:
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.id] = counts.get(task.id, 0) + 1
    return [
        DoctorFinding(
            code="duplicate_task_id",
            summary=f"task_id={task_id} count={count}",
            fix_command="litehive doctor --fix",
            autofixable=True,
        )
        for task_id, count in sorted(counts.items())
        if count > 1
    ]


def _task_status_findings(tasks: list[TaskRecord]) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    for task in tasks:
        if task.status == "merge_failed":
            findings.append(
                DoctorFinding(
                    code="merge_failed_task",
                    summary=f"task_id={task.id} title={task.title}",
                    fix_command=f"litehive debug {task.id} --worktree && litehive recover {task.id}",
                )
            )
        if task.status == "flagged":
            findings.append(
                DoctorFinding(
                    code="flagged_task",
                    summary=f"task_id={task.id} stage={task.pipeline_status} title={task.title}",
                    fix_command=f"litehive debug {task.id} && litehive queue promote {task.id}",
                )
            )
    return findings


def _commit_findings(tasks: list[TaskRecord], state: WorkspaceState | None) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    for task in tasks:
        if is_stranded_commit_task(task):
            findings.append(
                DoctorFinding(
                    code="commit_to_git_stuck",
                    summary=f"task_id={task.id} kind=stranded checkpoint_attempts={task.git.checkpoint_attempts}",
                    fix_command="litehive doctor --fix",
                    autofixable=state is not None,
                )
            )
            continue
        if state is not None and is_orphaned_commit_stage_task(task, state):
            findings.append(
                DoctorFinding(
                    code="commit_to_git_stuck",
                    summary=f"task_id={task.id} kind=orphaned status={task.status}",
                    fix_command="litehive doctor --fix",
                    autofixable=True,
                )
            )
    return findings


def _orphaned_subagent_findings(root: Path, tasks: list[TaskRecord]) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    for task in tasks:
        active = task.runtime.active_subagent
        if active is None:
            continue
        subagent_base = (root / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / active.path).resolve()
        orphaned = task.runtime.execution_status != "running" or not subagent_base.exists()
        if not orphaned:
            continue
        reason = "missing_artifacts" if not subagent_base.exists() else "stale_runtime_marker"
        findings.append(
            DoctorFinding(
                code="orphaned_subagent",
                summary=f"task_id={task.id} subagent_id={active.id} reason={reason}",
                fix_command="litehive doctor --fix",
                autofixable=True,
            )
        )
    return findings


def _stale_worktree_findings(root: Path, tasks: list[TaskRecord], state: WorkspaceState | None) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    managed_by_path: dict[str, TaskRecord] = {}
    active_task_id = None if state is None else state.active_task_id
    for task in tasks:
        worktree_rel = task.runtime.git.worktree_path or task.git.worktree_path
        if not is_managed_worktree_path(root, worktree_rel):
            continue
        worktree_path, changed = migrate_legacy_worktree(root, task)
        if changed:
            save_task_runtime(root, task)
            worktree_rel = task.runtime.git.worktree_path or task.git.worktree_path
        if worktree_rel is None:
            continue
        managed_by_path[worktree_rel] = task
        if worktree_path is None:
            continue
        if not worktree_path.exists():
            continue
        is_active = active_task_id == task.id
        if not is_active and not is_task_eligible_for_execution(task):
            findings.append(
                DoctorFinding(
                    code="stale_worktree",
                    summary=f"task_id={task.id} status={task.status} path={worktree_rel}",
                    fix_command="litehive worktree clean --dry-run && litehive worktree clean",
                )
            )
    for worktrees_root in (worktree_root(root), legacy_worktree_root(root)):
        if not worktrees_root.exists():
            continue
        for child in sorted(worktrees_root.iterdir()):
            if not child.is_dir():
                continue
            rel = str(child) if worktrees_root == worktree_root(root) else str(child.relative_to(root))
            if rel in managed_by_path:
                continue
            findings.append(
                DoctorFinding(
                    code="stale_worktree",
                    summary=f"task_id=missing path={rel}",
                    fix_command="litehive worktree clean --dry-run && litehive worktree clean",
                )
            )
    return findings


def _origin_divergence_finding(root: Path) -> DoctorFinding | None:
    message = check_origin_divergence(root)
    if message is None:
        return None
    return DoctorFinding(
        code="origin_divergence",
        summary=message,
        fix_command="git fetch origin main && git log --oneline --left-right main...origin/main",
    )


def status_attention_findings(root: Path, *, pool_stop_reason: str | None = None) -> list[str]:
    tasks = list_tasks(root)
    alerts: list[str] = []
    if pool_stop_reason == "diverged_from_origin":
        alerts.append(
            "pool halted: local main has diverged from origin/main and auto-recovery failed — manual reconciliation required"
        )
    for finding in _duplicate_id_findings(tasks):
        alerts.append(f"{finding.code}: {finding.summary} — run `{finding.fix_command}`")
    for finding in _task_status_findings(tasks):
        if finding.code == "merge_failed_task":
            alerts.append(f"merge_failed: {finding.summary}")
        elif finding.code == "flagged_task":
            alerts.append(f"flagged: {finding.summary}")
    return alerts


def scan_workspace_doctor(root: Path) -> DoctorReport:
    ensure_workspace(root)
    report = DoctorReport()
    state_file = state_path(root)
    state: WorkspaceState | None = None
    if state_file.exists():
        state_text = state_file.read_text(encoding="utf-8")
        if any(marker in state_text for marker in ("<<<<<<<", "=======", ">>>>>>>")):
            report.state_conflicted = True
            report.findings.append(
                DoctorFinding(
                    code="broken_state_yaml",
                    summary="path=.litehive/state.yaml reason=merge_conflict_markers",
                    fix_command=_state_edit_command(),
                )
            )
        else:
            try:
                payload = yaml.safe_load(state_text) or {}
                if not isinstance(payload, dict):
                    raise ValueError("top-level YAML value must be a mapping")
                state = WorkspaceState(**payload)
                report.stale_unmerged_worktrees_removed = _prune_stale_unmerged_worktrees(root, state)
            except (yaml.YAMLError, ValueError, TypeError) as exc:
                report.state_error = str(exc)
                report.findings.append(
                    DoctorFinding(
                        code="broken_state_yaml",
                        summary=f"path=.litehive/state.yaml reason={type(exc).__name__}",
                        fix_command=_state_edit_command(),
                    )
                )
    else:
        state = load_state(root)
        report.stale_unmerged_worktrees_removed = _prune_stale_unmerged_worktrees(root, state)

    tasks = list_tasks(root)
    report.findings.extend(_duplicate_id_findings(tasks))
    report.findings.extend(_task_status_findings(tasks))
    divergence = _origin_divergence_finding(root)
    if divergence is not None:
        report.findings.append(divergence)
    report.findings.extend(_stale_worktree_findings(root, tasks, state))
    report.findings.extend(_commit_findings(tasks, state))
    report.findings.extend(_orphaned_subagent_findings(root, tasks))
    return report


def _apply_repair_fix(root: Path) -> None:
    repair_workspace_state(root)


def _apply_orphaned_subagent_fix(root: Path) -> None:
    for task in list_tasks(root):
        active = task.runtime.active_subagent
        if active is None:
            continue
        subagent_base = (root / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / active.path).resolve()
        if task.runtime.execution_status == "running" and subagent_base.exists():
            continue
        task.runtime.active_subagent = None
        save_task_runtime(root, task)


def apply_doctor_fixes(root: Path) -> DoctorFixResult:
    initial = scan_workspace_doctor(root)
    repair_codes = {"duplicate_task_id", "commit_to_git_stuck"}
    if any(f.code in repair_codes and f.autofixable for f in initial.findings):
        _apply_repair_fix(root)
    if any(f.code == "orphaned_subagent" and f.autofixable for f in initial.findings):
        _apply_orphaned_subagent_fix(root)
    final = scan_workspace_doctor(root)
    remaining_keys = {(finding.code, finding.summary) for finding in final.findings}
    fixed = [
        finding for finding in initial.findings if (finding.code, finding.summary) not in remaining_keys
    ]
    return DoctorFixResult(
        fixed=fixed,
        remaining=final.findings,
        stale_unmerged_worktrees_removed=initial.stale_unmerged_worktrees_removed,
    )
