"""
Workspace-level engine usage and quota monitoring.

Records per-engine invocation counters, success/failure counts,
and the most recent quota/limit signal so ``litehive status``
and the engine-selection loop can see what the agents actually
consumed without re-probing every provider on every call.
Persistence goes through the workspace mutation guard so
concurrent stages cannot interleave half-written rows.
"""

import json
import sqlite3

from litehive.config.paths import workspace_path
from heru.base import CLIExecutionResult, ExternalCLIAdapter
from heru.types import EngineLimitKind
from litehive.domain.common import utcnow
from litehive.domain.engine import (
    EngineUsageObservation,
    EngineUsageRecord,
    EngineUsageWindow,
    WorkspaceEngineMonitoring,
)
from litehive.state.locking import workspace_mutation_guard_for_workspace
from litehive.workspace import Workspace


def load_engine_monitoring(workspace: Workspace) -> WorkspaceEngineMonitoring:
    """
    Return the per-engine usage/quota snapshot for this workspace.

    Returns an empty snapshot when the table does not exist
    yet (fresh workspace). Called by
    :func:`record_engine_execution` and
    :func:`record_engine_observation` so updates merge with
    the existing record, and by status/diagnostics readers.
    """
    return _load_engine_monitoring_from_db(workspace)


def save_engine_monitoring(workspace: Workspace, monitoring: WorkspaceEngineMonitoring) -> None:
    """
    Replace the entire ``engine_monitoring`` table under the mutation guard.

    Called by the record helpers after they merge a new
    observation. The workspace mutation guard guarantees
    concurrent stages cannot interleave half-written rows; a
    delete + reinsert under that lock is the simplest correct
    way to keep the in-memory aggregate as the source of truth.
    """
    with workspace_mutation_guard_for_workspace(workspace):
        _save_engine_monitoring_to_db(workspace, monitoring)


def _load_engine_monitoring_from_db(workspace: Workspace) -> WorkspaceEngineMonitoring:
    """
    Read every ``engine_monitoring`` row through a read-only handle.

    Skips rows whose JSON payload is malformed so a single bad
    row cannot blank the snapshot. Read-only mode lets status
    surfaces query monitoring without competing with the writer
    for the workspace mutation guard.
    """
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
    """
    Truncate-and-rewrite ``engine_monitoring`` in one transaction.

    The wholesale replace is intentional: the in-memory
    aggregate is the source of truth and partial updates would
    race the readers. Inside a single SQL transaction,
    truncate-then-insert is atomic from any reader's
    perspective.
    """
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
    """
    Record a completed engine CLI invocation.

    Bumps invocation/success/failure counters and persists the
    latest usage observation. Called by the orchestrator runner
    once per stage execution so the workspace's quota/usage
    view in ``litehive status`` reflects what the agents
    actually consumed; without this, the dashboard would always
    lag behind real usage.
    """
    monitoring = load_engine_monitoring(workspace)
    observation = _extract_usage_observation(adapter, execution)
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


def _extract_usage_observation(
    adapter: ExternalCLIAdapter,
    execution: CLIExecutionResult,
) -> EngineUsageObservation:
    """Probe the adapter for an ``extract_usage_observation`` hook and coerce its return into a typed observation — adapters that don't implement the hook get an empty observation so downstream logic stays uniform."""
    extract_usage_observation = getattr(adapter, "extract_usage_observation", None)
    if not callable(extract_usage_observation):
        return EngineUsageObservation()
    extracted = extract_usage_observation(execution)
    if isinstance(extracted, EngineUsageObservation):
        return extracted
    return EngineUsageObservation()


def record_engine_observation(
    workspace: Workspace,
    task_id: str,
    engine_name: str,
    adapter: ExternalCLIAdapter,
    execution: CLIExecutionResult,
) -> WorkspaceEngineMonitoring:
    """
    Persist mid-execution usage telemetry without bumping invocation counts.

    Short-circuits when the observation carries no usage,
    limit, or metadata signal so an empty observation costs
    nothing. Called when an engine emits a streaming usage
    event the runner wants to capture before the final
    :func:`record_engine_execution` call lands.
    """
    monitoring = load_engine_monitoring(workspace)
    observation = _extract_usage_observation(adapter, execution)
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
    """
    Merge one observation into the per-engine usage record.

    Updates counters, last-seen quota state, metadata, and
    limit reasons. Shared core that both
    :func:`record_engine_execution` and
    :func:`record_engine_observation` call so the
    bump-vs-no-bump branching lives in exactly one place; the
    invariant that observations are commutative-modulo-counters
    is hard to maintain across two implementations.
    """
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


def _limit_kind(reason: str | None) -> EngineLimitKind | None:
    """
    Classify a free-form limit/failure reason into a structured kind.

    Returns ``budget``, ``rate``, ``capacity``, ``quota``, or
    ``None`` when no marker matches. The monitoring aggregate
    carries a structured kind alongside the human-readable
    reason so engine selection can act on the kind without
    re-parsing prose. Used by :func:`_apply_engine_observation`
    when the engine adapter did not supply its own ``limit_kind``.
    """
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
