"""Workspace doctor checks and narrow automated fixes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from litehive.config import ensure_workspace, state_path
from litehive.daemon._execution import _check_origin_divergence
from litehive.models import TaskRecord, WorkspaceState
from litehive.pipeline.recovery.detection import (
    _is_orphaned_commit_stage_task,
    _is_stranded_commit_task,
)
from litehive.pipeline.recovery.workspace_repair import repair_workspace_state
from litehive.tasks.crud import list_tasks, save_task_runtime
from litehive.tasks.persistence import load_state
from litehive.tasks.queue_ops import _is_task_eligible_for_execution


def _is_litehive_managed_worktree(worktree_rel: str | None) -> bool:
    if not worktree_rel:
        return False
    path = PurePosixPath(worktree_rel)
    return not path.is_absolute() and path.parts[:2] == (".litehive", "worktrees")


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


@dataclass(slots=True)
class DoctorFixResult:
    fixed: list[DoctorFinding] = field(default_factory=list)
    remaining: list[DoctorFinding] = field(default_factory=list)


def _state_edit_command() -> str:
    return "cp .litehive/state.yaml .litehive/state.yaml.bak && ${EDITOR:-vi} .litehive/state.yaml"


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
        if _is_stranded_commit_task(task):
            findings.append(
                DoctorFinding(
                    code="commit_to_git_stuck",
                    summary=f"task_id={task.id} kind=stranded checkpoint_attempts={task.git.checkpoint_attempts}",
                    fix_command="litehive doctor --fix",
                    autofixable=state is not None,
                )
            )
            continue
        if state is not None and _is_orphaned_commit_stage_task(task, state):
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
        if not _is_litehive_managed_worktree(worktree_rel):
            continue
        managed_by_path[worktree_rel] = task
        worktree_path = root / worktree_rel
        if not worktree_path.exists():
            continue
        is_active = active_task_id == task.id
        if not is_active and not _is_task_eligible_for_execution(task):
            findings.append(
                DoctorFinding(
                    code="stale_worktree",
                    summary=f"task_id={task.id} status={task.status} path={worktree_rel}",
                    fix_command="litehive worktree clean --dry-run && litehive worktree clean",
                )
            )
    worktrees_root = root / ".litehive" / "worktrees"
    if worktrees_root.exists():
        for child in sorted(worktrees_root.iterdir()):
            if not child.is_dir():
                continue
            rel = str(child.relative_to(root))
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
    message = _check_origin_divergence(root)
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
    return DoctorFixResult(fixed=fixed, remaining=final.findings)
