"""
Probe functions that turn workspace state into status issues.

Each ``_probe_*`` examines one slice of workspace state and
produces zero or more :class:`StatusIssue` instances.
``litehive status`` and ``litehive health`` aggregate the
results so the operator sees one diagnostic line per fault.
The probes are deliberately small and read-only: they never
mutate workspace state and never raise on the status path.
"""

import json
from pathlib import Path
import sqlite3
import tomllib
from typing import Mapping

from pydantic import ValidationError

from litehive.daemon.logs import DaemonLogs
from litehive.daemon.registry import DaemonRegistry
from litehive.domain.common import PipelineStatus, RuntimeStageStatus, TaskExecutionStatus, TaskStatus
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.git.ops import check_origin_divergence
from litehive.observability.status_io import _heartbeat_age_seconds
from litehive.observability.status_types import (
    StatusIssue,
    _RECOVERY_FAILURE_FLAG_REASONS,
    _RECOVERY_FAILURE_STATE_REASONS,
    _RESUMABLE_PIPELINE_STAGES,
    _STATUS_WEDGED_HEARTBEAT_SECONDS,
    _TASKS_UNAVAILABLE_KEYS,
    _TRUSTED_STAGE_MARKER_STATUSES,
    _RecoveryFailureContext,
)
from litehive.state.records import WorkspaceTasks
from litehive.state.locking import runner_metadata_present, runner_pid_is_alive
from litehive.workspace import Workspace


def _probe_runner_state_impl(
    workspace: Workspace,
    state: WorkspaceState,
    runner: RunnerStatusState,
) -> list[StatusIssue]:
    """
    Detect a wedged runner or a stale runner lock.

    "Wedged" means a live PID but a stale heartbeat — the
    runner is alive but stuck. "Stale" means an active task
    record exists but the recorded PID is dead. Surfacing both
    tells the operator whether to wait (wedged - restart soon),
    or run ``litehive repair`` (stale - clean up).
    """
    issues: list[StatusIssue] = []
    active_task_id = runner.active_task_id or state.active_task_id
    live_pid = runner_pid_is_alive(runner.pid)
    lock_path = workspace.runtime_path("runtime", ".runner.lock")

    if live_pid:
        heartbeat_age_seconds = _heartbeat_age_seconds(runner.heartbeat_at)
        if heartbeat_age_seconds is not None and heartbeat_age_seconds > _STATUS_WEDGED_HEARTBEAT_SECONDS:
            stale_minutes = max(1, int(heartbeat_age_seconds // 60))
            issues.append(
                StatusIssue(
                    key="runner_state",
                    severity="ERROR",
                    message=(
                        f"WEDGED (heartbeat {stale_minutes} min stale)"
                        " — restart the runner or daemon so a fresh heartbeat is written."
                    ),
                )
            )
        return issues

    if (lock_path.exists() and runner_metadata_present(runner)) or active_task_id is not None:
        task_label = active_task_id or "-"
        issues.append(
            StatusIssue(
                key="runner_state",
                severity="ERROR",
                message=(
                    f"STALE (no live pid for active_task_id={task_label})"
                    " — clear the stale runner lock with `litehive repair` or restart the workspace daemon."
                ),
            )
        )
    return issues


def _probe_daemon_status_impl(workspace: Workspace) -> list[StatusIssue]:
    """
    Flag a daemon whose lockfile says ``stale`` with a dead PID.

    The combination means the daemon supervisor crashed without
    cleaning up; surfacing it prompts the operator to restart
    rather than leave the workspace looking idle while no
    daemon is actually scheduling work. Bad metadata is also
    surfaced so the operator can fix the lock file directly.
    """
    try:
        entry = DaemonRegistry(workspace).metadata()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [
            StatusIssue(
                key="daemon_status",
                severity="ERROR",
                message=(
                    f"BROKEN daemon metadata ({type(exc).__name__}: {exc})"
                    " — remove or rewrite the daemon lock metadata, then restart the daemon."
                ),
            )
        ]
    if entry is None or entry.status != "stale":
        return []
    if entry.pid is not None and not runner_pid_is_alive(entry.pid):
        return [
            StatusIssue(
                key="daemon_status",
                severity="ERROR",
                message=(
                    f"STOPPED (pid {entry.pid} not alive) — restart the daemon with `litehive start` or `litehive restart`."
                ),
            )
        ]
    return []


def _probe_last_cycle_impl(workspace: Workspace) -> list[StatusIssue]:
    """
    Catch a stalled cycle where repair tracebacked and the daemon stopped.

    Looks for a ``*-repair.log`` with a traceback that has no
    matching ``*-run.log`` follow-up — the run-all cycle never
    re-ran. Surfacing this points the operator at the failed log
    instead of leaving the workspace looking healthy when in
    fact the daemon is silently broken.
    """
    latest_dir = DaemonLogs(workspace).latest_run_all_dir()
    if latest_dir is None:
        return []
    repair_logs = sorted(latest_dir.glob("*-repair.log"))
    if not repair_logs:
        return []
    latest_repair = repair_logs[-1]
    prefix = latest_repair.name.split("-", 1)[0]
    if (latest_dir / f"{prefix}-run.log").exists():
        return []
    try:
        repair_text = latest_repair.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "Traceback" not in repair_text:
        return []
    return [
        StatusIssue(
            key="last_cycle",
            severity="ERROR",
            message=(
                f"FAILED at {latest_dir.name}, check {latest_repair}"
                " — inspect the traceback, run `litehive repair`, then restart the daemon if needed."
            ),
        )
    ]


def _probe_heru_link_impl(workspace: Workspace) -> list[StatusIssue]:
    """
    Detect a broken local heru source path in pyproject.

    A ``[tool.uv.sources].heru.path`` that no longer resolves
    on disk breaks every worktree's ``uv sync`` and would only
    show up as obscure failures inside agent runs. Surfacing it
    here names the missing path before the next agent run dies
    on resolve.
    """
    pyproject_path = workspace.root / "pyproject.toml"
    if not pyproject_path.exists():
        return []
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    heru_source = (((data.get("tool") or {}).get("uv") or {}).get("sources") or {}).get("heru")
    if not isinstance(heru_source, Mapping):
        return []
    configured_path = heru_source.get("path")
    if not isinstance(configured_path, str) or not configured_path.strip():
        return []
    candidate = Path(configured_path).expanduser()
    if candidate.is_absolute():
        resolved = candidate
    else:
        resolved = workspace.root / candidate
    if resolved.exists():
        return []
    return [
        StatusIssue(
            key="heru_link",
            severity="ERROR",
            message=(
                "BROKEN (worktrees cannot resolve heru)"
                f" — update `[tool.uv.sources].heru.path` or restore {resolved}, then run `uv sync`."
            ),
        )
    ]


def _probe_origin_divergence_impl(workspace: Workspace, state: WorkspaceState) -> list[StatusIssue]:
    """
    Re-surface a previous main/origin-main divergence stop.

    When the pool stopped on ``diverged_from_origin``, the
    underlying git divergence persists across daemon restarts;
    the operator must reconcile manually before the pool can
    resume. Re-rendering the diff each status read keeps the
    blocker visible until the operator clears it.
    """
    if state.pool_stop_reason != "diverged_from_origin":
        return []
    message = check_origin_divergence(workspace.root)
    if message is not None:
        detail = message
    else:
        detail = "local main and origin/main previously diverged; manual reconciliation is still required."
    return [
        StatusIssue(
            key="origin_divergence",
            severity="ERROR",
            message=(f"!!! ATTENTION REQUIRED !!! {detail}"),
        )
    ]


def _probe_pool_stop_reason(state: WorkspaceState) -> list[StatusIssue]:
    """
    Show a banner when the pool auto-stopped on consecutive failures.

    Operators otherwise wait for queued tasks that the scheduler
    will never start; the consecutive-failure stop is sticky
    until the operator clears it. The "3" floor on the failure
    count matches :func:`_emit_consecutive_task_failure_stop`
    so a stale counter does not produce a misleading "stopped
    after 0" message.
    """
    if state.pool_stop_reason != "consecutive_task_failures":
        return []
    failure_count = max(3, int(state.consecutive_task_failures))
    return [
        StatusIssue(
            key="critical_status",
            severity="ERROR",
            message=(
                f"CRITICAL: pool stopped after {failure_count} consecutive task failures"
                " — inspect the latest flagged tasks, fix the blocker, then clear the stop reason."
            ),
        )
    ]


def _probe_task_index_references_impl(
    workspace: Workspace,
    state: WorkspaceState,
    state_issues: list[StatusIssue],
) -> list[StatusIssue]:
    """
    Catch queue and active references pointing at unknown task ids.

    Without this probe ``health`` would show a clean queue
    while the scheduler keeps tripping on phantom ids; the
    SQLite task index is the source of truth for what tasks
    exist, and queue entries that disagree are bugs that warrant
    a database reconcile.
    """
    if any(issue.key in _TASKS_UNAVAILABLE_KEYS for issue in state_issues):
        return []
    db_path = workspace.runtime_path("data.db")
    if not db_path.exists():
        return []
    try:
        from litehive.state.rebuild_safety import sqlite_task_ids  # noqa: PLC0415

        db_ids = sqlite_task_ids(db_path)
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        return [
            StatusIssue(
                key="task_index",
                severity="ERROR",
                message=(
                    f"UNAVAILABLE ({type(exc).__name__}: {exc})"
                    " — restore/reconcile the workspace database before trusting queue references."
                ),
            )
        ]
    referenced_ids = set(state.queue)
    if state.active_task_id is not None:
        referenced_ids.add(state.active_task_id)
    missing_ids = tuple(sorted(referenced_ids - db_ids))
    if not missing_ids:
        return []
    sample = ", ".join(missing_ids[:10])
    if len(missing_ids) <= 10:
        suffix = ""
    else:
        suffix = f", ... ({len(missing_ids)} total)"
    return [
        StatusIssue(
            key="task_index",
            severity="ERROR",
            message=(
                f"SQLite task index is missing {len(missing_ids)} queued/active task reference(s): {sample}{suffix}"
                " — restore/reconcile the workspace database or remove stale queue references before scheduling."
            ),
        )
    ]


def _probe_task_status_damage(
    workspace: Workspace,
    state: WorkspaceState,
    runner: RunnerStatusState,
    state_issues: list[StatusIssue],
) -> list[StatusIssue]:
    """
    Walk every task and surface recovery-failure or backlog-damage.

    Calls out individual stuck tasks so ``health`` flags them
    before they silently block the queue. Skipped when earlier
    state probes already failed to load the task index — there
    is nothing to walk safely. Sorts the per-task issues by
    task id so successive runs produce a stable diff.
    """
    if any(issue.key in _TASKS_UNAVAILABLE_KEYS for issue in state_issues):
        return []
    if not workspace.runtime_path("data.db").exists():
        return []
    try:
        tasks = WorkspaceTasks(workspace).list(strict=False)
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        return [
            StatusIssue(
                key="task_status",
                severity="ERROR",
                message=(
                    f"UNAVAILABLE ({type(exc).__name__}: {exc})"
                    " — restore/reconcile task records before trusting task status diagnostics."
                ),
            )
        ]

    issues: list[StatusIssue] = []
    active_task_id = runner.active_task_id or state.active_task_id
    active_stage = _live_active_pipeline_stage(active_task_id, tasks)
    queued_ids = set(state.queue)

    for task in sorted(tasks, key=lambda candidate: candidate.id):
        recovery_issue = _recovery_failure_issue(workspace, task)
        if recovery_issue is not None:
            issues.append(recovery_issue)
        backlog_issue = _backlog_damage_issue(
            task,
            queued_ids=queued_ids,
            active_task_id=active_task_id,
            active_stage=active_stage,
        )
        if backlog_issue is not None:
            issues.append(backlog_issue)
    return issues


def _live_active_pipeline_stage(active_task_id: str | None, tasks: list[TaskRecord]) -> str | None:
    """
    Return the pipeline stage the active task is currently running.

    Used by backlog-damage probes to avoid flagging a queued
    sibling whose ``pipeline_status`` legitimately matches an
    in-flight stage. Looks at runtime execution status and the
    current-stage status because both must agree on "running"
    before we trust the stage as live.
    """
    if active_task_id is None:
        return None
    active_task = next((task for task in tasks if task.id == active_task_id), None)
    if active_task is None:
        return None
    current_stage = active_task.runtime.pipeline.current_stage
    if (
        active_task.runtime.pipeline.execution_status == TaskExecutionStatus.RUNNING
        or current_stage.status == RuntimeStageStatus.RUNNING
    ):
        return current_stage.stage or active_task.pipeline_status
    return None


def _recovery_failure_issue(workspace: Workspace, task: TaskRecord) -> StatusIssue | None:
    """
    Build a recovery-failure issue for a task whose recovery is exhausted.

    Tells the operator which task to evidence and requeue
    rather than leaving the task silently flagged. Composes
    the message from the lifecycle persistence layer so the
    issue carries the same detail the scheduler saw, not just
    the surface-level flag string.
    """
    if task.flag_reason is None:
        flag_reason = None
    else:
        flag_reason = str(task.flag_reason)
    context = _recovery_failure_context(workspace, task)
    if flag_reason not in _RECOVERY_FAILURE_FLAG_REASONS and context.failed_reason is None:
        return None

    stage = _task_issue_stage(task, context.origin_stage)
    reason = context.failed_reason or flag_reason or "recovery_failed"
    detail = (
        context.explanation
        or task.runtime.pipeline.last_outcome.reason
        or "recovery could not return the task to a runnable state"
    )
    return StatusIssue(
        key="recovery_failure",
        severity="ERROR",
        message=(
            f"Task {task.id} has recovery failure ({reason}) at `{stage}`: {detail}"
            f" — run `litehive task evidence {task.id}`, inspect recovery state,"
            f" then `litehive queue requeue {task.id}` when it is ready to continue."
        ),
    )


def _recovery_failure_context(workspace: Workspace, task: TaskRecord) -> _RecoveryFailureContext:
    """
    Pull recovery failure reason, explanation, and origin stage.

    Reads the lifecycle persistence layer so the recovery-failure
    issue carries the same detail the scheduler saw, not just
    the surface flag string. Tolerates a missing/corrupted
    pipeline-state row by emitting a synthesized context that
    still flags the failure.
    """
    context = _RecoveryFailureContext()
    from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound  # noqa: PLC0415

    try:
        state = SqlitePersistence(workspace).load(task.id)
    except TaskNotFound:
        return context
    except (OSError, sqlite3.DatabaseError, ValueError, ValidationError) as exc:
        context.failed_reason = "recovery_state_unavailable"
        context.explanation = f"Recovery state unavailable ({type(exc).__name__}: {exc})"
        return context

    failed_reason = None
    if state.failed_reason is not None:
        if hasattr(state.failed_reason, "value"):
            failed_reason = state.failed_reason.value
        else:
            failed_reason = str(state.failed_reason)
    if failed_reason in _RECOVERY_FAILURE_STATE_REASONS:
        context.failed_reason = failed_reason
    context.explanation = state.recovery_failure_explanation or state.failed_message
    trigger = state.active_recovery_trigger
    if trigger is None and state.recovery_history:
        trigger = state.recovery_history[-1].trigger
    if trigger is not None and trigger.origin_stage is not None:
        context.origin_stage = trigger.origin_stage
    return context


def _task_issue_stage(task: TaskRecord, preferred_stage: str | None = None) -> str:
    """
    Pick the most informative stage label to attach to a task issue.

    Prefers an explicit ``preferred_stage``, then falls back
    through ``last_outcome``, ``current_stage``, and
    ``pipeline_status``. The fallback chain ensures the message
    points at the stage that actually broke, not at a stale
    ``pipeline_status`` that has since drifted.
    """
    if preferred_stage:
        return preferred_stage
    return str(
        task.runtime.pipeline.last_outcome.stage
        or task.current_pipeline_stage
        or task.pipeline_status
        or "-"
    )


def _backlog_damage_issue(
    task: TaskRecord,
    queued_ids: set[str],
    active_task_id: str | None,
    active_stage: str | None,
) -> StatusIssue | None:
    """
    Detect inconsistencies between status, pipeline_status, and resume markers.

    Examples: a task in ``queued/backlog`` that runtime says
    should resume mid-pipeline; a queued task at a stale
    pipeline stage with no resume marker. The scheduler would
    silently normalize these away — surfacing them here lets
    the operator decide whether the normalization is correct or
    a real ``litehive repair`` is needed first.
    """
    if task.id == active_task_id:
        return None
    status = task.status
    pipeline_status = task.pipeline_status
    if status not in {TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.INTERRUPTED}:
        return None
    missing_from_queue = status == TaskStatus.QUEUED and task.id not in queued_ids

    runtime_stage = _runtime_resume_stage(task)
    if pipeline_status == PipelineStatus.BACKLOG and runtime_stage is not None:
        if missing_from_queue:
            queue_detail = " It is missing from WorkspaceState.queue, so the scheduler will not see it."
        else:
            queue_detail = ""
        return StatusIssue(
            key="backlog_damage",
            severity="ERROR",
            message=(
                f"Task {task.id} is {status}/backlog but runtime says resume from `{runtime_stage}`"
                f" — run `litehive repair` before starting unrelated work, or resume the task with"
                f" `litehive queue resume {task.id}`.{queue_detail}"
            ),
        )

    if (
        status == "queued"
        and pipeline_status not in {PipelineStatus.BACKLOG, PipelineStatus.DONE, active_stage}
        and not _task_has_resume_marker(task)
    ):
        return StatusIssue(
            key="backlog_damage",
            severity="WARN",
            message=(
                f"Task {task.id} is queued at stale pipeline_status=`{pipeline_status}` with no resume marker"
                " — the queue will normalize it back to backlog; run `litehive repair` now if this was unexpected."
            ),
        )
    return None


def _runtime_resume_stage(task: TaskRecord) -> str | None:
    """
    Return the resume stage the task's runtime markers point at.

    Checks the interruption record first (most explicit signal),
    then the current-stage marker if its status is in the
    trusted set. Used by backlog-damage detection to tell a
    legitimately-backlog task apart from one with a stale
    resume marker pointing into the pipeline.
    """
    candidates: list[str | None] = []
    interruption = task.runtime.execution.interruption
    if interruption is not None:
        candidates.extend([interruption.resume_stage, interruption.pipeline_status, interruption.stage])
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        candidates.append(current_stage.stage)
    for candidate in candidates:
        if candidate is not None and str(candidate) in _RESUMABLE_PIPELINE_STAGES:
            return str(candidate)
    return None


def _task_has_resume_marker(task: TaskRecord) -> bool:
    """
    Tell whether a queued task has a trusted resume marker.

    Either the current-stage marker or an interruption record
    must anchor the task's ``pipeline_status``. Without this
    check the backlog-damage probe would emit warnings for
    tasks the scheduler can legitimately resume — false alarms
    that would erode operator trust in the diagnostics.
    """
    stage = task.pipeline_status
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.stage == stage and current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        return True
    interruption = task.runtime.execution.interruption
    if interruption is not None and (interruption.resume_stage == stage or interruption.pipeline_status == stage):
        return True
    return False
