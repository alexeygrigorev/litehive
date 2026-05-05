"""Read-only loaders for config/state/runner/monitoring used by the `litehive status` snapshot."""

from dataclasses import asdict, fields
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from pydantic import ValidationError

from litehive.config.loading import merge_config_layers
from litehive.config.model import LitehiveConfig, validate_config_data
from litehive.config.paths import litehive_root, workspace_path
from litehive.config.workspace_files import config_path
from litehive.domain.common import RunnerStatus
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import WorkspaceState
from litehive.observability.engine_monitoring import load_engine_monitoring
from litehive.observability.status_io import (
    _safe_json_mapping,
    _safe_yaml_mapping,
    _validation_error_label,
)
from litehive.observability.status_types import StatusIssue
from litehive.state.locking import runner_metadata_present, runner_pid_is_alive
from litehive.state.store import runtime_store


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
