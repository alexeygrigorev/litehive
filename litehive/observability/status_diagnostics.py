"""Read-only diagnostics for `litehive status`."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import tomllib
from typing import Any, Literal, Mapping

import yaml
from pydantic import ValidationError

from litehive.config.loading import merge_config_layers
from litehive.config.model import LitehiveConfig
from litehive.config.paths import litehive_root, workspace_path
from litehive.config.registry import workspace_registry_error, workspace_registry_path
from litehive.config.workspace_files import config_path
from litehive.daemon.logs import latest_run_all_log_dir
from litehive.daemon.registry import daemon_metadata, pid_is_alive
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import TaskRecord, WorkspaceState
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
_RESUMABLE_PIPELINE_STAGES = {"grooming", "implementing", "testing", "accepting", "commit_to_git"}
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
        *_probe_origin_divergence(root, state),
        *_probe_task_status_damage(root, state, runner, state_issues),
    ]
    return StatusSnapshot(
        config=config,
        state=state,
        runner=runner,
        monitoring=monitoring,
        issues=issues,
    )


def status_has_problems(issues: list[StatusIssue]) -> bool:
    return any(issue.severity in {"WARN", "ERROR"} for issue in issues)


def render_health_summary(issues: list[StatusIssue]) -> str:
    broken = sum(1 for issue in issues if issue.severity == "ERROR")
    warning = sum(1 for issue in issues if issue.severity == "WARN")
    return f"health: {broken} broken, {warning} warning"


def render_issue_lines(issues: list[StatusIssue]) -> list[str]:
    if not status_has_problems(issues):
        return []
    return [*(issue.render() for issue in issues), render_health_summary(issues)]


def probe_registry_files() -> list[StatusIssue]:
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
    _, issue = _safe_yaml_document(
        litehive_root() / "daemons.yaml",
        key="registry",
        remediation="Fix or remove the daemon registry YAML, then restart the daemon if daemon tracking is needed.",
    )
    if issue is not None:
        issues.append(issue)
    return issues


def _load_config_for_status(root: Path) -> tuple[LitehiveConfig, list[StatusIssue]]:
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
        config = LitehiveConfig(**data)
    except (TypeError, ValidationError) as exc:
        issues.append(
            StatusIssue(
                key="config",
                severity="ERROR",
                message=(
                    f"INVALID merged config ({_validation_error_label(exc)})"
                    " — fix invalid config values; status is falling back to defaults."
                ),
            )
        )
        config = LitehiveConfig()
    return config, issues


def _load_state_for_status(root: Path) -> tuple[WorkspaceState, list[StatusIssue]]:
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
        store_state = runtime_store(root).load_workspace_state()
    except Exception as exc:
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
    from litehive.observability.engine_monitoring import load_engine_monitoring

    try:
        return load_engine_monitoring(root), []
    except Exception as exc:
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
    entry = daemon_metadata(root)
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
    resolved = candidate if candidate.is_absolute() else (root / candidate)
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
    if state.pool_stop_reason != "diverged_from_origin":
        return []
    from litehive.daemon.execution import check_origin_divergence

    message = check_origin_divergence(root)
    detail = (
        message
        if message is not None
        else "local main and origin/main previously diverged; manual reconciliation is still required."
    )
    return [
        StatusIssue(
            key="origin_divergence",
            severity="ERROR",
            message=(f"!!! ATTENTION REQUIRED !!! {detail}"),
        )
    ]


def _probe_task_status_damage(
    root: Path,
    state: WorkspaceState,
    runner: RunnerStatusState,
    state_issues: list[StatusIssue],
) -> list[StatusIssue]:
    if any(issue.key in _TASKS_UNAVAILABLE_KEYS for issue in state_issues):
        return []
    try:
        from litehive.state.records import list_tasks

        tasks = list_tasks(root, strict=False)
    except Exception:
        return []

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
    flag_reason = None if task.flag_reason is None else str(task.flag_reason)
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
            f" — run `litehive task debug {task.id} --worktree`, inspect recovery diagnostics,"
            f" then `litehive queue requeue {task.id}` when it is ready to continue."
        ),
    )


def _recovery_failure_context(root: Path, task: TaskRecord) -> _RecoveryFailureContext:
    context = _RecoveryFailureContext()
    try:
        from litehive.lifecycle.persistence import SqlitePersistence

        state = SqlitePersistence(root).load(task.id)
    except Exception:
        return context

    failed_reason = None
    if state.failed_reason is not None:
        failed_reason = (
            state.failed_reason.value if hasattr(state.failed_reason, "value") else str(state.failed_reason)
        )
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
    *,
    queued_ids: set[str],
    active_task_id: str | None,
    active_stage: str | None,
) -> StatusIssue | None:
    if task.id == active_task_id:
        return None
    status = str(task.status)
    pipeline_status = str(task.pipeline_status)
    if status not in {"queued", "in_progress", "interrupted"}:
        return None
    missing_from_queue = status == "queued" and task.id not in queued_ids

    runtime_stage = _runtime_resume_stage(task)
    if pipeline_status == "backlog" and runtime_stage is not None:
        queue_detail = (
            " It is missing from WorkspaceState.queue, so the scheduler will not see it."
            if missing_from_queue
            else ""
        )
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
        and pipeline_status not in {"backlog", "done", active_stage}
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
    candidates: list[str | None] = []
    interruption = task.runtime.execution.interruption
    if interruption is not None:
        candidates.extend([interruption.resume_stage, interruption.pipeline_status, interruption.stage])
    handoff = task.runtime.execution.continuation_handoff
    if handoff is not None:
        candidates.append(handoff.stage)
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        candidates.append(current_stage.stage)
    for candidate in candidates:
        if candidate is not None and str(candidate) in _RESUMABLE_PIPELINE_STAGES:
            return str(candidate)
    return None


def _task_has_resume_marker(task: TaskRecord) -> bool:
    stage = str(task.pipeline_status)
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.stage == stage and current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        return True
    interruption = task.runtime.execution.interruption
    if interruption is not None and (interruption.resume_stage == stage or interruption.pipeline_status == stage):
        return True
    handoff = task.runtime.execution.continuation_handoff
    if handoff is not None and handoff.stage == stage:
        return True
    return False


def _safe_yaml_mapping(
    path: Path,
    *,
    key: str,
    remediation: str,
) -> tuple[dict[str, Any] | None, StatusIssue | None]:
    data, issue = _safe_yaml_document(path, key=key, remediation=remediation)
    if issue is not None:
        return None, issue
    if data is None:
        return {}, None
    if not isinstance(data, Mapping):
        return {}, None
    return dict(data), None


def _safe_yaml_document(
    path: Path,
    *,
    key: str,
    remediation: str,
) -> tuple[object | None, StatusIssue | None]:
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
    path = workspace_path(root, "runtime", ".runner.lock")
    mapping, issue = _safe_yaml_mapping(
        path,
        key="runner_state",
        remediation="Remove or rewrite the runner lock file, then restart the runner or daemon.",
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
        return status.model_copy(update={"status": "running"}), None
    if runner_metadata_present(status):
        return status.model_copy(update={"status": "stale"}), None
    return RunnerStatusState(), None


def _heartbeat_age_seconds(heartbeat_at: str | None) -> int | None:
    if not heartbeat_at:
        return None
    try:
        timestamp = datetime.fromisoformat(heartbeat_at)
    except ValueError:
        return None
    return max(0, int((datetime.now(UTC) - timestamp).total_seconds()))


def _validation_error_label(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        error = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = error.get("msg") or "validation error"
        return f"{location}: {message}" if location else str(message)
    return str(exc).strip() or type(exc).__name__


def _yaml_location_label(exc: yaml.YAMLError | None) -> str:
    if exc is None:
        return "line unknown"
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return "line unknown"
    return f"line {mark.line + 1}"
