"""Shared task observability formatting helpers.

This module is the public observability/status renderer surface. The actual
formatting logic lives in sibling modules:

* ``status_summary`` — per-task summary helpers (engine/stage labels, durations,
  ``render_task_summary``).
* ``status_dashboard`` — dashboard-style sections used by ``litehive workspace status``.
* ``status_health`` — health-mode renderers used by ``litehive workspace health``.

This module owns the pipeline-status orchestration
(``TaskPipelineStatusCollector`` / ``render_task_pipeline_status_lines``) and re-exports the rest so existing
``from litehive.observability.status import ...`` imports keep working.
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from litehive.attention import OperatorAttentionProjector
from litehive.config.engine_freezes import active_engine_freezes
from litehive.config.model import LitehiveConfig
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import TaskIntentRecord, TaskRecord, TaskStateRecord, WorkspaceState
from litehive.observability.status_dashboard import (
    _EVENT_LABELS,  # noqa: F401  (re-export)
    collect_recent_activity,
    find_last_completed_task,
    render_active_task_section,
    render_active_tasks_section,
    render_last_completed_section,
    render_queue_section,
    render_recent_activity_section,
)
from litehive.observability.status_diagnostics import (
    StatusIssue,
    StatusSnapshotCollector,
)
from litehive.observability.status_health import (
    render_health_active_task_lines,
    render_health_daemon_lines,
    render_health_flagged_task_lines,
    render_health_quota_lines,
    render_health_recent_completion_lines,
    render_health_worktree_finding_lines,
    render_health_worktree_lines,
)
from litehive.observability.status_summary import (
    _PIPELINE_STAGES,  # noqa: F401  (re-export)
    _collect_report_durations,  # noqa: F401  (re-export)
    _duration_label,  # noqa: F401  (re-export)
    _latest_stage_failure_classification,  # noqa: F401  (re-export)
    _latest_stage_report_for_task,  # noqa: F401  (re-export)
    _pid_label,  # noqa: F401  (re-export)
    _sandbox_label,  # noqa: F401  (re-export)
    _seconds_label,  # noqa: F401  (re-export)
    _task_engine_label,
    _task_last_summary_label,  # noqa: F401  (re-export)
    _task_last_verdict_label,  # noqa: F401  (re-export)
    _task_stage_label,
    estimate_task_execution,  # noqa: F401  (re-export)
    render_task_summary,
)
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace

__all__ = [
    "StatusIssue",
    "StatusRenderMode",
    "TaskPipelineStatusCollector",
    "TaskPipelineStatusData",
    "collect_recent_activity",
    "estimate_task_execution",
    "find_last_completed_task",
    "render_active_task_detail_lines",
    "render_active_task_section",
    "render_active_tasks_section",
    "render_detailed_status_header_lines",
    "render_engine_availability_lines",
    "render_health_active_task_lines",
    "render_health_daemon_lines",
    "render_health_flagged_task_lines",
    "render_health_quota_lines",
    "render_health_recent_completion_lines",
    "render_health_worktree_finding_lines",
    "render_health_worktree_lines",
    "render_last_completed_section",
    "render_queue_section",
    "render_recent_activity_section",
    "render_runner_status_line",
    "render_runtime_policy_lines",
    "render_task_pipeline_status_lines",
    "render_task_summary",
]


StatusRenderMode = Literal["summary", "detailed"]


@dataclass(slots=True)
class TaskPipelineStatusData:
    """
    Aggregated snapshot consumed by the CLI status renderer.

    Bundles workspace configuration, runner state, monitoring info, any
    open issues, the currently active task, queue head, waiting-lines
    text, and a human-readable runner state label into a single object
    so the renderer can operate without further database or filesystem
    lookups.
    """

    config: LitehiveConfig
    state: WorkspaceState
    runner: RunnerStatusState
    monitoring: WorkspaceEngineMonitoring
    issues: list[StatusIssue]
    active_task_id: str | None
    active_task: TaskRecord | None
    queue_head: str | None
    waiting_lines: list[str]
    runner_state_label: str


class TaskPipelineStatusCollector:
    """
    Workspace-bound collector for the task pipeline status view.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Bind the collector to the workspace whose pipeline status it
        will assemble.

        The workspace is used to read configuration, task records,
        runner state, and monitoring data during ``collect``.
        """
        self.workspace = workspace

    def collect(
        self,
        read_only: bool = False,
        diagnostics: bool = False,
    ) -> TaskPipelineStatusData:
        """
        Bundle the workspace snapshot the CLI status commands need in one read.

        Called by the CLI status entry points and by the daemon when
        emitting periodic health summaries. ``read_only=True`` opens
        SQLite in URI read-only mode so concurrent runners cannot be
        blocked by a status read; ``diagnostics=True`` widens the
        snapshot to include issue collection that the operational
        fast-path skips, because the issue scan is too expensive for
        every status print.
        """
        collector = StatusSnapshotCollector(self.workspace)
        if diagnostics:
            snapshot = collector.collect()
        else:
            snapshot = collector.collect_operational()
        active_task_id = snapshot.runner.active_task_id or snapshot.state.active_task_id
        if read_only:
            if active_task_id:
                active_task = _load_task_read_only_impl(self.workspace, active_task_id)
            else:
                active_task = None
            waiting_lines = OperatorAttentionProjector(self.workspace).waiting_lines(reconcile=False)
        else:
            if active_task_id:
                active_task = WorkspaceTasks(self.workspace).get(active_task_id)
            else:
                active_task = None
            waiting_lines = OperatorAttentionProjector(self.workspace).waiting_lines()
        if not diagnostics:
            waiting_lines = _operational_attention_lines(waiting_lines)
        return TaskPipelineStatusData(
            config=snapshot.config,
            state=snapshot.state,
            runner=snapshot.runner,
            monitoring=snapshot.monitoring,
            issues=snapshot.issues,
            active_task_id=active_task_id,
            active_task=active_task,
            queue_head=_first_or_none(snapshot.state.queue),
            waiting_lines=waiting_lines,
            runner_state_label=_runner_state_label_impl(self.workspace, snapshot.runner),
        )


def render_task_pipeline_status_lines(
    status: TaskPipelineStatusData,
    workspace: Path,
    mode: StatusRenderMode,
    retry_on_label: str | None = None,
) -> list[str]:
    """
    Format a collected pipeline snapshot into flat key/value lines.

    Called by the ``litehive status`` CLI handler and by the
    daemon's periodic status writer. The ``mode`` switch keeps
    both surfaces sharing one renderer instead of two parallel
    formatters that would drift over time. ``retry_on_label`` is
    required in ``detailed`` mode because the runtime-policy block
    needs the caller's preformatted label; refusing to default
    it makes the missing data the call site's problem rather
    than producing misleading output.
    """
    if mode == "detailed":
        lines = render_detailed_status_header_lines(workspace, status.config, status.state, status.runner)
    else:
        if status.active_task_id is not None:
            active_task_id_label = status.active_task_id
        else:
            active_task_id_label = "None"
        if status.state.pool_stop_reason is not None:
            pool_stop_reason_label = status.state.pool_stop_reason
        else:
            pool_stop_reason_label = "None"
        lines = [
            f"workspace: {workspace}",
            f"default_engine: {status.config.default_engine}",
            "mode: implementation",
            f"active_task_id: {active_task_id_label}",
            f"queued_tasks: {len(status.state.queue)}",
            f"pool_stop_reason: {pool_stop_reason_label}",
        ]

    lines.extend(status.waiting_lines)
    if status.queue_head is not None:
        lines.append(f"queue_head: {status.queue_head}")

    if mode == "summary":
        lines.append(f"runner_status: {status.runner_state_label}")
        if status.runner.pid is not None:
            lines.append(f"runner_pid: {status.runner.pid}")
        if status.runner.started_at:
            lines.append(f"runner_started_at: {status.runner.started_at}")
        if status.runner.heartbeat_at:
            lines.append(f"runner_heartbeat_at: {status.runner.heartbeat_at}")

    lines.extend(render_active_task_detail_lines(status.active_task, status.config.default_engine))
    lines.extend(render_engine_availability_lines(status.config, status.monitoring))

    if mode == "detailed":
        if retry_on_label is None:
            raise ValueError("retry_on_label is required when rendering detailed status output")
        lines.extend(render_runtime_policy_lines(status.config, retry_on_label))

    return lines


def _first_or_none(items):
    """
    Return the first element of a sequence or ``None`` when it is empty.

    Used so call sites that only care about the head of a list
    (e.g. ``queue_head`` for status output) can avoid inline
    ternaries; helps keep the snapshot construction one
    dictionary literal rather than a chain of ``if`` blocks.
    """
    if items:
        return items[0]
    return None


def _runner_state_label_impl(workspace: Workspace, runner: RunnerStatusState) -> str:
    """
    Distinguish never-started workspaces from stopped or dead runners.

    The detailed status path reads liveness from the runner record
    alone, but the summary path also needs to tell operators "you
    have not run litehive yet here" apart from "the runner died"
    — that distinction matters when an operator is debugging an
    apparently-stopped workspace. Probes the on-disk lockfile to
    pick the right label.
    """
    if runner.status in {"running", "late"}:
        return "running"
    if not workspace.runtime_path("runtime", ".runner.lock").exists():
        return "never_started"
    if runner.pid is None:
        return "stopped"
    return "dead"


def _load_task_read_only_impl(workspace: Workspace, task_id: str) -> TaskRecord | None:
    """
    Load a task without taking a writer lock.

    The standard :func:`get_task` path opens the workspace DB
    read-write and would contend with an active writer; status
    output must never block runtime work, so this opens SQLite
    in URI read-only mode and swallows OS/DB errors back to
    ``None``. Status is allowed to be incomplete, never to
    raise — a missing task is silently dropped from the snapshot.
    """
    db_path = workspace.runtime_path("data.db")
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            intent_row = connection.execute(
                "SELECT payload FROM task_intent WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            state_row = connection.execute(
                "SELECT payload FROM task_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if intent_row is None:
            return None
        intent = TaskIntentRecord.model_validate(json.loads(intent_row["payload"]))
        if state_row is None:
            state = None
        else:
            state = TaskStateRecord(**json.loads(state_row["payload"]))
        task = TaskRecord.from_intent_and_state(intent, state)
    except (OSError, sqlite3.DatabaseError, ValueError):
        return None
    return task


def _operational_attention_lines(lines: list[str]) -> list[str]:
    """
    Trim attention output to the ``operator_needed:*`` lines.

    Diagnostics mode emits the full attention list; the
    operational mode only wants the subset that signals "a human
    must act", so this drops everything else before the lines
    reach the CLI status renderer. Without this filter, the
    operator sees lower-priority attention noise mixed in with
    real action items.
    """
    if not lines:
        return []
    return [
        line
        for line in lines
        if line.startswith(("operator_needed:", "operator_needed_pool_stop_reason:", "operator_needed_tasks:"))
    ]


def render_active_task_detail_lines(task: TaskRecord | None, default_engine: str) -> list[str]:
    """
    Render the flat active-task detail block.

    Used by both the summary and detailed CLI status outputs so the
    "what is the active task doing right now?" lines are
    identical between modes. Returns an empty list when no task
    is active so the caller's ``lines.extend(...)`` is a no-op.
    """
    if task is None:
        return []

    engine = _task_engine_label(task, default_engine)
    stage = _task_stage_label(task)
    return [
        f"active_task_title: {task.title}",
        f"active_task_status: {task.status}/{task.pipeline_status}",
        f"active_stage: {stage}",
        f"active_engine: {engine}",
    ]


def render_runner_status_line(runner: RunnerStatusState, state: WorkspaceState | None = None) -> str:
    """
    Format the one-line ``runner_status: ...`` summary.

    Embedded in both CLI status output and daemon health logs;
    keeping the format identical lets operators correlate the
    two without translating between formats. ``state`` is
    accepted but unused — kept on the signature so the daemon
    caller can pass it unconditionally and so a future renderer
    can read workspace state without a breaking API change.
    """
    del state
    base_status = (
        f"runner_status: {runner.status} pid={runner.pid or '-'} "
        f"started_at={runner.started_at or '-'} "
        f"heartbeat_at={runner.heartbeat_at or '-'}"
    )
    return f"{base_status} active_task_id={runner.active_task_id or '-'}"


def render_detailed_status_header_lines(
    workspace: Path,
    config: LitehiveConfig,
    state: WorkspaceState,
    runner: RunnerStatusState,
) -> list[str]:
    """
    Emit the workspace/engine/runner/queue header for detailed-mode status.

    Called by :func:`render_task_pipeline_status_lines` for the
    detailed path; kept as a separate function so tests can exercise
    the header independently and so the daemon can render just
    the header for shorter health snapshots without dragging in
    the rest of the status body.
    """
    active_task_id = runner.active_task_id or state.active_task_id
    lines = [
        f"workspace: {workspace}",
        "status_read_mode: detailed",
        f"default_engine: {config.default_engine}",
    ]
    freezes = active_engine_freezes(config)
    if freezes:
        for engine_name, until_dt in sorted(freezes.items()):
            local_until = until_dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")
            lines.append(f"engine_frozen: {engine_name} until {local_until}")

    lines.extend(
        [
            f"litehive_source_path: {config.litehive_source_path or '-'}",
            f"active_task_id: {active_task_id}",
            render_runner_status_line(runner, state),
            f"queued_tasks: {len(state.queue)}",
            f"pool_stop_reason: {state.pool_stop_reason}",
        ]
    )
    return lines


def render_runtime_policy_lines(config: LitehiveConfig, retry_on_label: str) -> list[str]:
    """
    Render the retry/pool/process-profile policy block.

    Lets operators see effective runtime settings without
    reading config files; needed when triaging why the pool
    behaved differently across runs. ``retry_on_label`` is
    preformatted by the caller (the CLI) because the
    human-readable spelling depends on which retry-on enum
    members are active and the formatter lives next to the CLI
    parsing — this renderer just stitches it in.
    """
    return [
        f"default_retry_limit: {config.default_retry_limit}",
        f"retry_on: {retry_on_label}",
        f"pool_stop_on_failure: {config.pool_stop_on_failure}",
        f"pool_max_tasks: {config.pool_max_tasks}",
        f"pool_stop_on_dirty_git: {config.pool_stop_on_dirty_git}",
        f"pool_stop_on_attention: {config.pool_stop_on_attention}",
        f"process_profile: {config.process_profile}",
    ]


def render_engine_availability_lines(
    config: LitehiveConfig,
    monitoring: WorkspaceEngineMonitoring,
) -> list[str]:
    """
    Emit one ``engine_available:`` line per engine the workspace knows about.

    Marks quota, freeze, and rate-limit pressure so operators
    see the state of every engine before kicking off a run.
    Unions every engine the workspace touches (default,
    preference list, freeze list, live monitoring) and annotates
    each — that union is what makes the CLI status output and
    the workspace-health command share one source of truth.
    """
    engine_names = {config.default_engine, *config.engine_preference, *config.engine_freeze}
    engine_names.update(monitoring.engines)
    if not engine_names:
        return []

    frozen = active_engine_freezes(config)
    lines: list[str] = []
    for engine_name in sorted(engine_names):
        record = monitoring.engines.get(engine_name)
        status = "available"
        detail = ""
        if engine_name in frozen:
            status = "frozen"
            detail = f" until={config.engine_freeze.get(engine_name, '-')}"
        elif record is not None and record.last_limit_kind in {"quota", "rate", "budget"}:
            status = str(record.last_limit_kind)
        if engine_name == config.default_engine:
            default_marker = " default=yes"
        else:
            default_marker = ""
        lines.append(f"engine_available: {engine_name} status={status}{default_marker}{detail}")
    return lines
