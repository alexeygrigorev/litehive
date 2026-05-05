"""Read-only diagnostics for `litehive status`."""

from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import tomllib
from typing import Any, Literal, Mapping

import yaml
from pydantic import ValidationError

from litehive.config.loading import merge_config_layers
from litehive.config.model import LitehiveConfig, validate_config_data
from litehive.config.paths import litehive_root, workspace_path
from litehive.config.registry import workspace_registry_error, workspace_registry_path
from litehive.config.workspace_files import config_path
from litehive.daemon.logs import latest_run_all_log_dir
from litehive.daemon.registry import daemon_metadata, pid_is_alive
from litehive.domain.common import PipelineStatus, RunnerStatus, TaskStage, TaskStatus
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.observability.engine_monitoring import load_engine_monitoring
from litehive.state.store import runtime_store
from litehive.state.locking import runner_metadata_present, runner_pid_is_alive

_STATUS_WEDGED_HEARTBEAT_SECONDS = 10 * 60
_RECOVERY_FAILURE_FLAG_REASONS = {
    "crash_budget_exhausted",
    "recovery_budget_exhausted",
    "recovery_failed",
}
_RECOVERY_FAILURE_STATE_REASONS = {
    "pre_exec_recovery_failed",
    "recovery_budget_hit",
    "recovery_crashed",
    "recovery_exhausted",
    "recovery_missing_target_stage",
}
_RESUMABLE_PIPELINE_STAGES: frozenset[TaskStage] = frozenset(
    {TaskStage.GROOMING, TaskStage.IMPLEMENTING, TaskStage.TESTING, TaskStage.ACCEPTING, TaskStage.COMMIT_TO_GIT}
)
_TRUSTED_STAGE_MARKER_STATUSES = {"idle", "paused", "interrupted", "running"}
_TASKS_UNAVAILABLE_KEYS = {"state"}

StatusSeverity = Literal["WARN", "ERROR"]


@dataclass(slots=True)
class StatusIssue:
    key: str
    severity: StatusSeverity
    message: str

    def render(self) -> str:
        return f"{self.key}: {self.message}"


@dataclass(slots=True)
class StatusSnapshot:
    config: LitehiveConfig
    state: WorkspaceState
    runner: RunnerStatusState
    monitoring: WorkspaceEngineMonitoring
    issues: list[StatusIssue]


@dataclass(slots=True)
class _RecoveryFailureContext:
    failed_reason: str | None = None
    explanation: str | None = None
    origin_stage: str | None = None


def collect_status_snapshot(root: Path) -> StatusSnapshot:
    """Build the full diagnostic snapshot used by `litehive health` to surface every probe finding."""
    root = root.resolve()
    registry_issues = probe_registry_files()
    config, config_issues = _load_config_for_status(root)
    state, state_issues = _load_state_for_status(root)
    runner, runner_issue = _load_runner_status_for_status(root)
    monitoring, monitoring_issues = _load_engine_monitoring_for_status(root)
    issues = [
        *registry_issues,
        *config_issues,
        *state_issues,
        *([runner_issue] if runner_issue is not None else []),
        *monitoring_issues,
        *_probe_runner_state(root, state, runner),
        *_probe_daemon_status(root),
        *_probe_last_cycle(root),
        *_probe_heru_link(root),
        *_probe_pool_stop_reason(state),
        *_probe_origin_divergence(root, state),
        *_probe_task_index_references(root, state, state_issues),
        *_probe_task_status_damage(root, state, runner, state_issues),
    ]
    return StatusSnapshot(
        config=config,
        state=state,
        runner=runner,
        monitoring=monitoring,
        issues=issues,
    )


def collect_operational_status_snapshot(root: Path) -> StatusSnapshot:
    """Collect the small read-only status view used by default status output."""
    root = root.resolve()
    config, config_issues = _load_config_for_status(root)
    state, state_issues = _load_state_for_status(root)
    runner, runner_issue = _load_runner_status_for_status(root)
    monitoring, monitoring_issues = _load_engine_monitoring_for_status(root)
    issues = [
        *config_issues,
        *state_issues,
        *([runner_issue] if runner_issue is not None else []),
        *monitoring_issues,
        *_probe_runner_state(root, state, runner),
        *_probe_pool_stop_reason(state),
    ]
    return StatusSnapshot(
        config=config,
        state=state,
        runner=runner,
        monitoring=monitoring,
        issues=issues,
    )


def status_has_problems(issues: list[StatusIssue]) -> bool:
    """Decide whether the status command should print an issues block and exit non-zero."""
    return any(issue.severity in {"WARN", "ERROR"} for issue in issues)


def render_health_summary(issues: list[StatusIssue]) -> str:
    broken = sum(1 for issue in issues if issue.severity == "ERROR")
    warning = sum(1 for issue in issues if issue.severity == "WARN")
    return f"health: {broken} broken, {warning} warning"


def render_issue_lines(issues: list[StatusIssue]) -> list[str]:
    """Render the verbose issue block (full message + remediation) shown by `litehive health`."""
    if not status_has_problems(issues):
        return []
    return [*(issue.render() for issue in issues), render_health_summary(issues)]


def render_operational_issue_lines(issues: list[StatusIssue]) -> list[str]:
    """Render the terse issue block used by the default `litehive status`, with remediation hints stripped."""
    if not status_has_problems(issues):
        return []
    return [
        *(f"{issue.key}: {_operational_issue_message(issue.message)}" for issue in issues),
        render_health_summary(issues),
    ]


def _operational_issue_message(message: str) -> str:
    """Trim the ` — remediation…` tail off a status message so default `status` output stays one-line per issue."""
    return message.split(" — ", 1)[0]


def probe_registry_files() -> list[StatusIssue]:
    """Surface a broken global workspace registry so health output explains why no workspace can be resolved."""
    issues: list[StatusIssue] = []
    registry_error = workspace_registry_error()
    if registry_error is not None:
        issues.append(
            StatusIssue(
                key="registry",
                severity="ERROR",
                message=(
                    f"BROKEN at {workspace_registry_path()} ({registry_error})"
                    " — restore or remove the global workspace registry database so Litehive can rebuild it."
                ),
            )
        )
    return issues


def _load_config_for_status(root: Path) -> tuple[LitehiveConfig, list[StatusIssue]]:
    """Merge global+workspace config like normal load, but downgrade YAML/validation errors into status issues
    instead of raising so `status`/`health` can still render with whatever fields are valid."""
    issues: list[StatusIssue] = []
    data = asdict(LitehiveConfig())
    for path, key in ((litehive_root() / "config.yaml", "global_config"), (config_path(root), "config")):
        mapping, issue = _safe_yaml_mapping(
            path,
            key=key,
            remediation="Fix the YAML syntax or remove the file, then rerun `litehive status`.",
        )
        if issue is not None:
            issues.append(issue)
            continue
        if mapping is not None:
            data = merge_config_layers(data, mapping)
    try:
        config = LitehiveConfig(**_validate_status_config_data(data))
    except (TypeError, ValueError, ValidationError) as exc:
        issues.append(
            StatusIssue(
                key="config",
                severity="ERROR",
                message=(
                    f"INVALID merged config ({_validation_error_label(exc)})"
                    " — fix invalid config values; status is rendering with valid config fields only."
                ),
            )
        )
        config = _best_effort_status_config(data)
    return config, issues


def _validate_status_config_data(data: Mapping[str, Any]) -> dict[str, Any]:
    return validate_config_data(data)


def _best_effort_status_config(data: Mapping[str, Any]) -> LitehiveConfig:
    """Build a status-only config without letting one bad field hide valid values."""
    valid_keys = {field.name for field in fields(LitehiveConfig)}
    defaults = asdict(LitehiveConfig())
    remaining = {key: value for key, value in data.items() if key in valid_keys}
    while remaining:
        try:
            return LitehiveConfig(**remaining)
        except (TypeError, ValueError, ValidationError) as exc:
            bad_key = _config_error_key(exc)
            if bad_key is None or bad_key not in remaining:
                break
            remaining.pop(bad_key, None)
    return LitehiveConfig(**{key: value for key, value in remaining.items() if defaults.get(key) == value})


def _config_error_key(exc: Exception) -> str | None:
    """Extract the offending field name from a config validation/TypeError so the best-effort loader can drop
    just that field and retry instead of giving up the whole config."""
    if isinstance(exc, ValidationError):
        if exc.errors():
            error = exc.errors()[0]
        else:
            error = {}
        location = error.get("loc", ())
        if location:
            return str(location[0])
        return None
    message = str(exc)
    for field in fields(LitehiveConfig):
        if field.name in message:
            return field.name
    if "unexpected keyword argument" in message:
        if "'" in message:
            return message.rsplit("'", 2)[1]
        return None
    return None


def _load_state_for_status(root: Path) -> tuple[WorkspaceState, list[StatusIssue]]:
    """Read workspace state read-only and convert SQLite/validation failures into a status issue rather than
    propagating so `status`/`health` can still render the rest of the snapshot."""
    issues: list[StatusIssue] = []
    db_path = workspace_path(root, "data.db")
    if db_path.exists():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
                connection.execute("PRAGMA schema_version").fetchone()
        except sqlite3.DatabaseError as exc:
            detail = str(exc).strip() or type(exc).__name__
            issues.append(
                StatusIssue(
                    key="state",
                    severity="ERROR",
                    message=(
                        f"BROKEN at {db_path} ({detail})"
                        " — restore the workspace database from backup or rerun `litehive db migrate`."
                    ),
                )
            )
            return WorkspaceState(), issues
    try:
        store_state = runtime_store(root).load_workspace_state_read_only()
    except (OSError, sqlite3.DatabaseError, ValueError, ValidationError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        issues.append(
            StatusIssue(
                key="state",
                severity="ERROR",
                message=(
                    f"BROKEN at {db_path} ({detail})"
                    " — restore the workspace database from backup or rerun `litehive db migrate`."
                ),
            )
        )
        return WorkspaceState(), issues

    return (store_state or WorkspaceState()), issues


def _load_engine_monitoring_for_status(
    root: Path,
) -> tuple[WorkspaceEngineMonitoring, list[StatusIssue]]:
    """Load engine usage stats; on failure return an empty record plus a WARN so status output is not blocked
    by a corrupt engine_monitoring table."""
    try:
        return load_engine_monitoring(root), []
    except (OSError, sqlite3.DatabaseError, ValueError, ValidationError) as exc:
        issue = StatusIssue(
            key="engine_monitoring",
            severity="WARN",
            message=(
                f"BROKEN in workspace database ({type(exc).__name__}: {exc})"
                " — rerun `litehive db migrate` or restore engine usage details from backup."
            ),
        )
        return WorkspaceEngineMonitoring(), [issue]


def _probe_runner_state(root: Path, state: WorkspaceState, runner: RunnerStatusState) -> list[StatusIssue]:
    """Detect a wedged runner (live pid but stale heartbeat) or a stale runner lock (active task but no live pid),
    so the operator knows whether to wait, restart, or `litehive repair`."""
    issues: list[StatusIssue] = []
    active_task_id = runner.active_task_id or state.active_task_id
    live_pid = runner_pid_is_alive(runner.pid)
    lock_path = workspace_path(root, "runtime", ".runner.lock")

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


def _probe_daemon_status(root: Path) -> list[StatusIssue]:
    """Flag the daemon as STOPPED when its lockfile says stale and the recorded pid is dead, prompting a restart."""
    try:
        entry = daemon_metadata(root)
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
    if entry is None or entry.get("status") != "stale":
        return []
    pid = entry.get("pid")
    if isinstance(pid, int) and not pid_is_alive(pid):
        return [
            StatusIssue(
                key="daemon_status",
                severity="ERROR",
                message=(
                    f"STOPPED (pid {pid} not alive) — restart the daemon with `litehive start` or `litehive restart`."
                ),
            )
        ]
    return []


def _probe_last_cycle(root: Path) -> list[StatusIssue]:
    """Catch the case where the most recent run-all cycle ended with a repair traceback and never re-ran,
    so health output points the operator at the failed log instead of looking healthy."""
    latest_dir = latest_run_all_log_dir(root)
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


def _probe_heru_link(root: Path) -> list[StatusIssue]:
    """Detect a `[tool.uv.sources].heru.path` that no longer resolves on disk — that breaks every worktree's
    `uv sync`, so health output names the missing path before the next agent run dies."""
    pyproject_path = root / "pyproject.toml"
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
        resolved = (root / candidate)
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


def _probe_origin_divergence(root: Path, state: WorkspaceState) -> list[StatusIssue]:
    """Re-surface a previous main/origin-main divergence stop with the current diff so the operator knows
    manual reconciliation is still pending before the pool can run."""
    if state.pool_stop_reason != "diverged_from_origin":
        return []
    # inline: daemon.execution top-level-imports observability.status, which
    # top-level-imports this module (would cycle).
    from litehive.daemon.execution import check_origin_divergence  # noqa: PLC0415

    message = check_origin_divergence(root)
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
    """Show a CRITICAL banner when the pool auto-stopped after the consecutive-failures cap was hit, so
    operators don't wait for tasks that the scheduler will never start."""
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


def _probe_task_index_references(
    root: Path,
    state: WorkspaceState,
    state_issues: list[StatusIssue],
) -> list[StatusIssue]:
    """Catch queue/active references that point at task ids missing from the SQLite task table — without this
    `health` would show a clean queue while the scheduler keeps tripping on phantom ids."""
    if any(issue.key in _TASKS_UNAVAILABLE_KEYS for issue in state_issues):
        return []
    db_path = workspace_path(root, "data.db")
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
    root: Path,
    state: WorkspaceState,
    runner: RunnerStatusState,
    state_issues: list[StatusIssue],
) -> list[StatusIssue]:
    """Walk every task and emit recovery-failure / backlog-damage issues so `health` calls out individual stuck
    tasks before they silently block the queue."""
    if any(issue.key in _TASKS_UNAVAILABLE_KEYS for issue in state_issues):
        return []
    if not workspace_path(root, "data.db").exists():
        return []
    try:
        from litehive.state.records import list_tasks  # noqa: PLC0415

        tasks = list_tasks(root, strict=False)
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
        recovery_issue = _recovery_failure_issue(root, task)
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
    """Return the stage the active task is genuinely running, used by backlog-damage probes to avoid flagging
    a queued sibling that legitimately matches the in-flight stage."""
    if active_task_id is None:
        return None
    active_task = next((task for task in tasks if task.id == active_task_id), None)
    if active_task is None:
        return None
    current_stage = active_task.runtime.pipeline.current_stage
    if str(active_task.runtime.pipeline.execution_status) == "running" or current_stage.status == "running":
        return str(current_stage.stage or active_task.pipeline_status)
    return None


def _recovery_failure_issue(root: Path, task: TaskRecord) -> StatusIssue | None:
    """Build an ERROR issue for a task whose recovery budget/attempt failed, so `health` tells the operator
    which task to evidence and requeue rather than leaving it silently flagged."""
    if task.flag_reason is None:
        flag_reason = None
    else:
        flag_reason = str(task.flag_reason)
    context = _recovery_failure_context(root, task)
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


def _recovery_failure_context(root: Path, task: TaskRecord) -> _RecoveryFailureContext:
    """Pull the recovery failure reason / explanation / origin stage out of the lifecycle persistence layer
    so the recovery-failure issue carries the same detail the scheduler saw, not just the flag string."""
    context = _RecoveryFailureContext()
    try:
        from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound  # noqa: PLC0415

        state = SqlitePersistence(root).load(task.id)
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
        context.origin_stage = str(trigger.origin_stage)
    return context


def _task_issue_stage(task: TaskRecord, preferred_stage: str | None = None) -> str:
    """Pick the most informative stage label to attach to a task issue, falling back through last_outcome,
    current_stage, pipeline_status — so the message points at the stage that actually broke."""
    if preferred_stage:
        return preferred_stage
    return str(
        task.runtime.pipeline.last_outcome.stage
        or task.runtime.pipeline.current_stage.stage
        or task.pipeline_status
        or "-"
    )


def _backlog_damage_issue(
    task: TaskRecord,
    queued_ids: set[str],
    active_task_id: str | None,
    active_stage: str | None,
) -> StatusIssue | None:
    """Detect inconsistencies between TaskStatus, pipeline_status, and resume markers — e.g. a task in
    `queued/backlog` that runtime says should resume mid-pipeline — that the scheduler would silently
    normalize away if not surfaced here."""
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
    """Return the stage the task's runtime markers say it should resume from (if any of them point to a
    resumable stage), used by backlog-damage detection to tell `backlog` apart from a stale resume marker."""
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
    """Tell whether a queued task has any trusted marker (current stage or interruption) anchoring its
    pipeline_status, so the backlog-damage probe doesn't WARN on tasks the scheduler can legitimately resume."""
    stage = str(task.pipeline_status)
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.stage == stage and current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        return True
    interruption = task.runtime.execution.interruption
    if interruption is not None and (interruption.resume_stage == stage or interruption.pipeline_status == stage):
        return True
    return False


def _safe_yaml_mapping(
    path: Path,
    key: str,
    remediation: str,
) -> tuple[dict[str, Any] | None, StatusIssue | None]:
    """Read a YAML file expected to be a top-level mapping; on parse error or wrong shape return a status
    issue instead of raising, so the config loader can keep merging the layers it could read."""
    data, issue = _safe_yaml_document(path, key=key, remediation=remediation)
    if issue is not None:
        return None, issue
    if data is None:
        return {}, None
    if not isinstance(data, Mapping):
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"INVALID at {path} (expected YAML mapping) — {remediation}",
        )
    return dict(data), None


def _safe_yaml_document(
    path: Path,
    key: str,
    remediation: str,
) -> tuple[object | None, StatusIssue | None]:
    """Read a YAML file and turn YAMLError/OSError into a status issue with location info, so corrupt config
    surfaces as one diagnostic line instead of crashing `status`/`health`."""
    if not path.exists():
        return None, None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), None
    except yaml.YAMLError as exc:
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"CORRUPT at {path} ({_yaml_location_label(exc)}) — {remediation}",
        )
    except OSError as exc:
        detail = exc.strerror or str(exc)
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"UNREADABLE at {path} ({detail}) — {remediation}",
        )


def _load_runner_status_for_status(root: Path) -> tuple[RunnerStatusState, StatusIssue | None]:
    """Parse the runner lockfile and reconcile its recorded pid against the OS so the status snapshot reflects
    RUNNING/STALE/empty, returning a status issue on bad JSON instead of raising."""
    path = workspace_path(root, "runtime", ".runner.lock")
    mapping, issue = _safe_json_mapping(
        path,
        key="runner_state",
        remediation="Remove or rewrite the runner lock file as JSON, then restart the runner or daemon.",
    )
    if issue is not None:
        return RunnerStatusState(), issue
    if not mapping:
        return RunnerStatusState(), None
    try:
        status = RunnerStatusState(**mapping)
    except ValidationError as exc:
        return RunnerStatusState(), StatusIssue(
            key="runner_state",
            severity="ERROR",
            message=(
                f"INVALID at {path} ({_validation_error_label(exc)})"
                " — rewrite the runner lock file or restart the runner or daemon."
            ),
        )
    if runner_pid_is_alive(status.pid):
        return status.model_copy(update={"status": RunnerStatus.RUNNING}), None
    if runner_metadata_present(status):
        return status.model_copy(update={"status": RunnerStatus.STALE}), None
    return RunnerStatusState(), None


def _safe_json_mapping(
    path: Path,
    key: str,
    remediation: str,
) -> tuple[dict[str, Any] | None, StatusIssue | None]:
    """Read a JSON file expected to be an object; on missing/blank file return empty, on parse/shape errors
    return a status issue so the runner-state loader can degrade gracefully."""
    if not path.exists():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}, None
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"CORRUPT at {path} (line {exc.lineno}) — {remediation}",
        )
    except OSError as exc:
        detail = exc.strerror or str(exc)
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"UNREADABLE at {path} ({detail}) — {remediation}",
        )
    if data is None:
        return {}, None
    if not isinstance(data, Mapping):
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"INVALID at {path} (expected JSON object) — {remediation}",
        )
    return dict(data), None


def _heartbeat_age_seconds(heartbeat_at: str | None) -> int | None:
    """Convert the runner's last-heartbeat ISO timestamp into seconds-since-now, used by the runner-state probe
    to decide WEDGED."""
    if not heartbeat_at:
        return None
    try:
        timestamp = datetime.fromisoformat(heartbeat_at)
    except ValueError:
        return None
    return max(0, int((datetime.now(UTC) - timestamp).total_seconds()))


def _validation_error_label(exc: Exception) -> str:
    """Format the first pydantic ValidationError (or any other exception) as a one-line `path: msg` label so
    config/runner status messages stay scannable instead of dumping pydantic's full multi-line traceback."""
    if isinstance(exc, ValidationError):
        if exc.errors():
            error = exc.errors()[0]
        else:
            error = {}
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = error.get("msg") or "validation error"
        if location:
            return f"{location}: {message}"
        return str(message)
    return str(exc).strip() or type(exc).__name__


def _yaml_location_label(exc: yaml.YAMLError | None) -> str:
    """Render the line number from a YAMLError (or `line unknown`) so corrupt-YAML status messages point
    at the offending line."""
    if exc is None:
        return "line unknown"
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return "line unknown"
    return f"line {mark.line + 1}"
