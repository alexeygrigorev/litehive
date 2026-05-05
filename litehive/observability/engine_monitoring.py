"""Workspace-level engine usage and quota monitoring."""

import json
import sqlite3

from litehive.config.paths import workspace_path
from heru.base import CLIExecutionResult, ExternalCLIAdapter
from litehive.domain.common import utcnow
from litehive.domain.engine import (
    EngineUsageObservation,
    EngineUsageRecord,
    EngineUsageWindow,
    WorkspaceEngineMonitoring,
)
from litehive.state.locking import workspace_mutation_guard
from litehive.workspace import Workspace


def load_engine_monitoring(workspace: Workspace) -> WorkspaceEngineMonitoring:
    """Return the per-engine usage/quota snapshot persisted for this workspace, or an empty snapshot when the table does not exist yet; called by :func:`record_engine_execution` and :func:`record_engine_observation` (so updates merge with the existing record) and by status/diagnostics readers."""
    return _load_engine_monitoring_from_db(workspace)


def save_engine_monitoring(workspace: Workspace, monitoring: WorkspaceEngineMonitoring) -> None:
    """Replace the entire ``engine_monitoring`` table contents under the workspace mutation guard; called by the record helpers after they merge a new observation, with the lock guaranteeing concurrent stages cannot interleave half-written rows."""
    with workspace_mutation_guard(workspace.root):
        _save_engine_monitoring_to_db(workspace, monitoring)


def _load_engine_monitoring_from_db(workspace: Workspace) -> WorkspaceEngineMonitoring:
    """Read every ``engine_monitoring`` row over a read-only sqlite handle and rebuild the :class:`WorkspaceEngineMonitoring` aggregate, skipping rows whose JSON payload is malformed; the read-only mode lets status surfaces query monitoring without competing with the writer for the workspace lock."""
    db_path = workspace_path(workspace.root, "data.db")
    if not db_path.exists():
        return WorkspaceEngineMonitoring()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'engine_monitoring'",
        ).fetchone()
        if table is None:
            return WorkspaceEngineMonitoring()
        rows = connection.execute(
            "SELECT engine_name, payload FROM engine_monitoring ORDER BY engine_name ASC",
        ).fetchall()
    engines = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            engines[str(row["engine_name"])] = payload
    return WorkspaceEngineMonitoring(engines=engines)


def _save_engine_monitoring_to_db(workspace: Workspace, monitoring: WorkspaceEngineMonitoring) -> None:
    """Truncate-and-rewrite the ``engine_monitoring`` table inside one transaction; the wholesale-replace strategy is intentional because the in-memory aggregate is the source of truth and partial updates would race the readers."""
    with workspace.connect() as connection:
        connection.execute("DELETE FROM engine_monitoring")
        for engine_name, record in monitoring.engines.items():
            connection.execute(
                """
                INSERT INTO engine_monitoring (engine_name, updated_at, payload)
                VALUES (?, ?, ?)
                """,
                (
                    engine_name,
                    record.observed_at or utcnow(),
                    json.dumps(record.model_dump(mode="json"), sort_keys=True),
                ),
            )
        connection.commit()


def record_engine_execution(
    workspace: Workspace,
    task_id: str,
    engine_name: str,
    adapter: ExternalCLIAdapter,
    execution: CLIExecutionResult,
    failure_kind: str | None,
    failure_reason: str | None,
) -> WorkspaceEngineMonitoring:
    """Bump invocation/success/failure counters and persist the latest usage observation after an engine CLI run completes; called by the orchestrator runner once per stage execution so the workspace's quota/usage view in ``litehive status`` reflects what the agents actually consumed."""
    monitoring = load_engine_monitoring(workspace)
    extract_usage_observation = getattr(adapter, "extract_usage_observation", None)
    observation = (
        extract_usage_observation(execution) if callable(extract_usage_observation) else None
    ) or EngineUsageObservation()
    monitoring = _apply_engine_observation(
        monitoring,
        engine_name=engine_name,
        task_id=task_id,
        execution=execution,
        observation=observation,
        count_invocation=True,
        failure_kind=failure_kind,
        failure_reason=failure_reason,
    )
    save_engine_monitoring(workspace, monitoring)
    return monitoring


def record_engine_observation(
    workspace: Workspace,
    task_id: str,
    engine_name: str,
    adapter: ExternalCLIAdapter,
    execution: CLIExecutionResult,
) -> WorkspaceEngineMonitoring:
    """Persist mid-execution usage telemetry without bumping the invocation counter, short-circuiting when there is nothing useful to record; called when an engine emits a streaming usage event the runner wants to capture before the final ``record_engine_execution`` call."""
    monitoring = load_engine_monitoring(workspace)
    extract_usage_observation = getattr(adapter, "extract_usage_observation", None)
    observation = (
        extract_usage_observation(execution) if callable(extract_usage_observation) else None
    ) or EngineUsageObservation()
    if (
        observation.usage is None
        and observation.limit_reason is None
        and not observation.metadata
        and observation.provider is None
    ):
        return monitoring
    monitoring = _apply_engine_observation(
        monitoring,
        engine_name=engine_name,
        task_id=task_id,
        execution=execution,
        observation=observation,
        count_invocation=False,
        failure_kind=None,
        failure_reason=None,
    )
    save_engine_monitoring(workspace, monitoring)
    return monitoring


def _apply_engine_observation(
    monitoring: WorkspaceEngineMonitoring,
    engine_name: str,
    task_id: str,
    execution: CLIExecutionResult,
    observation: EngineUsageObservation,
    count_invocation: bool,
    failure_kind: str | None,
    failure_reason: str | None,
) -> WorkspaceEngineMonitoring:
    """Merge one observation into the per-engine usage record (counters, last-seen quota state, metadata, limit reasons) and return the updated aggregate; the shared core both ``record_engine_execution`` and ``record_engine_observation`` call so the bump-vs-no-bump branching lives in exactly one place."""
    record = monitoring.engines.get(engine_name)
    if record is None:
        record = EngineUsageRecord(engine=engine_name)

    observed_at = observation.observed_at or utcnow()
    record.source = observation.source
    if observation.provider:
        record.provider = observation.provider
    record.observed_at = observed_at
    record.last_task_id = task_id
    if count_invocation:
        record.invocation_count += max(1, observation.invocation_count)

    if count_invocation:
        success = observation.success
        if success is None:
            success = failure_kind is None and execution.exit_code == 0
        if success:
            record.success_count += 1
        else:
            record.failure_count += 1

    limit_reason = observation.limit_reason or failure_reason
    limit_kind = observation.limit_kind or _limit_kind(limit_reason)
    if count_invocation and limit_reason is not None and limit_kind is not None:
        record.limit_event_count += 1
    if limit_reason is not None and limit_kind is not None:
        record.last_limit_reason = limit_reason
        record.last_limit_kind = limit_kind

    if observation.usage is not None:
        record.usage = observation.usage
    elif count_invocation and record.source == "local":
        record.usage = EngineUsageWindow(
            used=record.invocation_count,
            unit="requests",
        )
    if observation.metadata:
        record.metadata = {**record.metadata, **observation.metadata}

    monitoring.engines[engine_name] = record
    return monitoring


def _limit_kind(reason: str | None) -> str | None:
    """Classify a free-form failure/limit reason into one of ``budget``/``rate``/``capacity``/``quota`` (or None when no marker matches), so the monitoring aggregate carries a structured kind alongside the human-readable reason; used by :func:`_apply_engine_observation` when the engine adapter did not supply its own ``limit_kind``."""
    if not reason:
        return None
    normalized = reason.lower()
    if any(marker in normalized for marker in ("budget", "credit", "insufficient funds")):
        return "budget"
    if any(marker in normalized for marker in ("rate limit", "too many requests")):
        return "rate"
    if "capacity" in normalized:
        return "capacity"
    if any(marker in normalized for marker in ("quota", "usage limit")):
        return "quota"
    return None
