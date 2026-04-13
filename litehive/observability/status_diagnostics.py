"""Read-only diagnostics for `litehive status`."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
import tomllib
from typing import Any, Literal, Mapping

import yaml

from litehive.config.loading import merge_config_layers
from litehive.config.model import LitehiveConfig
from litehive.config.paths import (
    config_path,
    daemon_registry_path,
    global_config_path,
    legacy_daemon_registry_path,
    legacy_workspace_registry_path,
    state_path,
    workspace_dir,
)
from litehive.daemon.logs import latest_run_all_log_dir
from litehive.daemon.registry import pid_is_alive
from litehive.models import RunnerStatusState, WorkspaceEngineMonitoring, WorkspaceState
from litehive.storage import runtime_store
from litehive.workspace.locking import runner_pid_is_alive

_STATUS_WEDGED_HEARTBEAT_SECONDS = 10 * 60

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


def collect_status_snapshot(root: Path) -> StatusSnapshot:
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
        *_probe_registry_health(),
        *_probe_runner_state(root, state, runner),
        *_probe_daemon_status(root),
        *_probe_last_cycle(root),
        *_probe_heru_link(root),
        *_probe_origin_divergence(root, state),
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


def _load_config_for_status(root: Path) -> tuple[LitehiveConfig, list[StatusIssue]]:
    issues: list[StatusIssue] = []
    data = asdict(LitehiveConfig())
    for path, key in ((global_config_path(), "global_config"), (config_path(root), "config")):
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
    except Exception:
        config = LitehiveConfig()
    return config, issues


def _load_state_for_status(root: Path) -> tuple[WorkspaceState, list[StatusIssue]]:
    issues: list[StatusIssue] = []
    yaml_state: WorkspaceState | None = None
    mapping, issue = _safe_yaml_mapping(
        state_path(root),
        key="state",
        remediation="Fix `.litehive/state.yaml` or remove it so Litehive can regenerate state metadata.",
    )
    if issue is not None:
        issues.append(issue)
    elif mapping is not None:
        try:
            yaml_state = WorkspaceState(**mapping)
        except Exception:
            yaml_state = None

    try:
        store_state = runtime_store(root).load_workspace_state()
    except Exception:
        store_state = None

    default_state = WorkspaceState()
    if yaml_state is not None and (store_state is None or store_state == default_state):
        return yaml_state, issues
    if store_state is not None:
        return store_state, issues
    if yaml_state is not None:
        return yaml_state, issues
    return default_state, issues


def _load_engine_monitoring_for_status(
    root: Path,
) -> tuple[WorkspaceEngineMonitoring, list[StatusIssue]]:
    path = workspace_dir(root) / "engine-monitoring.yaml"
    if not path.exists():
        return WorkspaceEngineMonitoring(), []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, Mapping):
            data = {}
        return WorkspaceEngineMonitoring(**data), []
    except (OSError, yaml.YAMLError) as exc:
        issue = StatusIssue(
            key="engine_monitoring",
            severity="WARN",
            message=(
                f"CORRUPT at {path} ({_yaml_location_label(exc if isinstance(exc, yaml.YAMLError) else None)})"
                " — remove or fix `.litehive/engine-monitoring.yaml` to restore engine usage details."
            ),
        )
        return WorkspaceEngineMonitoring(), [issue]
    except Exception:
        return WorkspaceEngineMonitoring(), []


def _probe_registry_health() -> list[StatusIssue]:
    issues: list[StatusIssue] = []
    for path in (legacy_workspace_registry_path(), legacy_daemon_registry_path(), daemon_registry_path()):
        _, issue = _safe_yaml_document(
            path,
            key="registry",
            remediation="Fix the YAML syntax or remove the registry file so Litehive can rebuild it.",
        )
        if issue is not None:
            issues.append(issue)
    return issues


def _probe_runner_state(root: Path, state: WorkspaceState, runner: RunnerStatusState) -> list[StatusIssue]:
    issues: list[StatusIssue] = []
    active_task_id = runner.active_task_id or state.active_task_id
    live_pid = runner_pid_is_alive(runner.pid)
    lock_path = workspace_dir(root) / ".runner.lock"

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

    if (lock_path.exists() and _runner_metadata_present(runner)) or active_task_id is not None:
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
    registry_path = daemon_registry_path()
    data, issue = _safe_yaml_document(
        registry_path,
        key="registry",
        remediation="Fix the YAML syntax or remove the registry file so Litehive can rebuild it.",
    )
    if issue is not None or not isinstance(data, Mapping):
        return []
    daemons = data.get("daemons")
    if not isinstance(daemons, Mapping):
        return []
    payload = daemons.get(str(root.resolve()))
    if not isinstance(payload, Mapping):
        return []
    pid = payload.get("pid")
    if isinstance(pid, int) and not pid_is_alive(pid):
        return [
            StatusIssue(
                key="daemon_status",
                severity="ERROR",
                message=(
                    f"STOPPED (pid {pid} not alive)"
                    " — restart the daemon with `litehive start` or `litehive restart`."
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
    heru_source = (
        (((data.get("tool") or {}).get("uv") or {}).get("sources") or {}).get("heru")
    )
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
            message=(
                "!!! ATTENTION REQUIRED !!! "
                f"{detail}"
            ),
        )
    ]


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
    path = workspace_dir(root) / ".runner.lock"
    mapping, issue = _safe_yaml_mapping(
        path,
        key="runner_state",
        remediation="Remove or rewrite `.litehive/.runner.lock`, then restart the runner or daemon.",
    )
    if issue is not None:
        return RunnerStatusState(), issue
    if not mapping:
        return RunnerStatusState(), None
    try:
        status = RunnerStatusState(**mapping)
    except Exception:
        return RunnerStatusState(), None
    if runner_pid_is_alive(status.pid):
        return status.model_copy(update={"status": "running"}), None
    if _runner_metadata_present(status):
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


def _runner_metadata_present(status: RunnerStatusState) -> bool:
    return any(
        (
            status.pid is not None,
            bool(status.workspace),
            bool(status.command),
            status.started_at is not None,
            status.heartbeat_at is not None,
            status.active_task_id is not None,
        )
    )


def _yaml_location_label(exc: yaml.YAMLError | None) -> str:
    if exc is None:
        return "line unknown"
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return "line unknown"
    return f"line {mark.line + 1}"
