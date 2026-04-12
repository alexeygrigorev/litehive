import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from litehive.db.schema import connect_workspace_db
from litehive.models import utcnow

from .types import FailedReason, NodeName, PipelineMode


@dataclass(frozen=True)
class Limits:
    stage_retry_limit: int = 3
    same_hook_reject_limit: int = 3
    same_engine_retry_limit: int = 3
    overall_retry_limit: int = 30
    grace_period_seconds: int = 120


@dataclass
class LastReport:
    files_changed: int = 0
    tests_added: int = 0


@dataclass
class HookRejectFingerprint:
    point: NodeName
    command: str
    description: str = ""
    fingerprint: str = ""


@dataclass
class LastRejection:
    """Most recent reject against a retry-eligible stage.

    Populated by the ``inc_stage_retry`` effect whenever a Reject event is
    being routed back into a retry. Read by ``RoleAgent.build_prompt`` so the
    next agent visit can see exactly what the previous attempt (or hook) was
    unhappy about.
    """

    source: str  # "agent" | "hook" | "guard" | "system"
    reason: str
    raised_at_phase: NodeName  # the phase that emitted the reject


@dataclass
class TaskState:
    """Single source of truth for task state the machine reads and writes.

    Guards, rule targets, and effect factories all receive a ``TaskState`` by
    convention read-only. The Runner is the only place that mutates it, via
    ``StateDelta`` patches.

    ``limits`` is runtime config (not persisted) — real persistence adapters
    should omit it on save and re-inject it on load.
    """

    task_id: str
    stage: NodeName
    pipeline_mode: PipelineMode
    stage_retry: dict[NodeName, int] = field(default_factory=dict)
    recovery_attempt: dict[NodeName, int] = field(default_factory=dict)
    pre_exec_recovery_attempt: int = 0
    origin_stage: NodeName | None = None
    failure_context: dict[str, Any] = field(default_factory=dict)
    last_report: LastReport = field(default_factory=LastReport)
    last_rejection_by_stage: dict[NodeName, LastRejection] = field(default_factory=dict)
    consecutive_same_hook_rejects: int = 0
    last_hook_reject_fingerprint: HookRejectFingerprint | None = None
    hook_reject_recovery_invoked: bool = False
    failed_reason: FailedReason | None = None
    failed_message: str | None = None
    limits: Limits = field(default_factory=Limits)


class Persistence(Protocol):
    def save(self, state: TaskState) -> None: ...
    def load(self, task_id: str) -> TaskState: ...


class InMemoryPersistence:
    def __init__(self) -> None:
        self._states: dict[str, TaskState] = {}

    def save(self, state: TaskState) -> None:
        self._states[state.task_id] = state

    def load(self, task_id: str) -> TaskState:
        return self._states[task_id]


# ── sqlite-backed persistence ────────────────────────────────────────────


def _state_payload(state: TaskState) -> dict[str, Any]:
    return {
        "stage_retry": dict(state.stage_retry),
        "recovery_attempt": dict(state.recovery_attempt),
        "pre_exec_recovery_attempt": state.pre_exec_recovery_attempt,
        "origin_stage": state.origin_stage,
        "failure_context": dict(state.failure_context),
        "last_report": {
            "files_changed": state.last_report.files_changed,
            "tests_added": state.last_report.tests_added,
        },
        "last_rejection_by_stage": {
            stage: {
                "source": rej.source,
                "reason": rej.reason,
                "raised_at_phase": rej.raised_at_phase,
            }
            for stage, rej in state.last_rejection_by_stage.items()
        },
        "consecutive_same_hook_rejects": state.consecutive_same_hook_rejects,
        "last_hook_reject_fingerprint": (
            {
                "point": state.last_hook_reject_fingerprint.point,
                "command": state.last_hook_reject_fingerprint.command,
                "description": state.last_hook_reject_fingerprint.description,
                "fingerprint": state.last_hook_reject_fingerprint.fingerprint,
            }
            if state.last_hook_reject_fingerprint is not None
            else None
        ),
        "hook_reject_recovery_invoked": state.hook_reject_recovery_invoked,
        "failed_reason": state.failed_reason,
        "failed_message": state.failed_message,
    }


def _state_from_row(
    task_id: str,
    stage: NodeName,
    pipeline_mode: str,
    payload: dict[str, Any],
    limits: Limits,
) -> TaskState:
    last_report_data = payload.get("last_report") or {}
    last_rejections_data = payload.get("last_rejection_by_stage") or {}
    hook_fingerprint_data = payload.get("last_hook_reject_fingerprint") or None
    return TaskState(
        task_id=task_id,
        stage=stage,
        pipeline_mode=PipelineMode(pipeline_mode),
        stage_retry=dict(payload.get("stage_retry") or {}),
        recovery_attempt=dict(payload.get("recovery_attempt") or {}),
        pre_exec_recovery_attempt=int(payload.get("pre_exec_recovery_attempt") or 0),
        origin_stage=payload.get("origin_stage"),
        failure_context=dict(payload.get("failure_context") or {}),
        last_report=LastReport(
            files_changed=int(last_report_data.get("files_changed", 0)),
            tests_added=int(last_report_data.get("tests_added", 0)),
        ),
        last_rejection_by_stage={
            stage_name: LastRejection(
                source=rej["source"],
                reason=rej["reason"],
                raised_at_phase=rej["raised_at_phase"],
            )
            for stage_name, rej in last_rejections_data.items()
        },
        consecutive_same_hook_rejects=int(payload.get("consecutive_same_hook_rejects") or 0),
        last_hook_reject_fingerprint=(
            HookRejectFingerprint(
                point=hook_fingerprint_data["point"],
                command=hook_fingerprint_data["command"],
                description=hook_fingerprint_data.get("description", ""),
                fingerprint=hook_fingerprint_data["fingerprint"],
            )
            if hook_fingerprint_data is not None
            else None
        ),
        hook_reject_recovery_invoked=bool(payload.get("hook_reject_recovery_invoked", False)),
        failed_reason=payload.get("failed_reason"),
        failed_message=payload.get("failed_message"),
        limits=limits,
    )


class TaskNotFound(LookupError):
    """Raised when ``SqlitePersistence.load`` is called on an unknown task id."""


class SqlitePersistence:
    """Persists ``TaskState`` to the ``pipeline_task_state`` sqlite table.

    The scalar fields (stage, pipeline_mode) are stored as columns so the
    daemon can query them directly without parsing JSON. Everything else
    (counters, failure_context, last_rejection_by_stage, last_report, failed_*)
    lives in the free-form ``payload`` column.

    ``limits`` is runtime config and is re-injected from the ``limits``
    constructor argument on every load — it never hits the db.
    """

    def __init__(self, workspace_root: Path, *, limits: Limits | None = None) -> None:
        self.workspace_root = workspace_root
        self.limits = limits or Limits()

    def save(self, state: TaskState) -> None:
        payload_json = json.dumps(_state_payload(state), sort_keys=True)
        with connect_workspace_db(self.workspace_root) as connection:
            connection.execute(
                """
                INSERT INTO pipeline_task_state (task_id, stage, pipeline_mode, payload, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    stage = excluded.stage,
                    pipeline_mode = excluded.pipeline_mode,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    state.task_id,
                    state.stage,
                    state.pipeline_mode.value,
                    payload_json,
                    utcnow(),
                ),
            )
            connection.commit()

    def load(self, task_id: str) -> TaskState:
        with connect_workspace_db(self.workspace_root) as connection:
            row = connection.execute(
                "SELECT stage, pipeline_mode, payload FROM pipeline_task_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise TaskNotFound(f"no pipeline_task_state row for task {task_id!r}")
        return _state_from_row(
            task_id=task_id,
            stage=row["stage"],
            pipeline_mode=row["pipeline_mode"],
            payload=json.loads(row["payload"]),
            limits=self.limits,
        )

    def initialize(
        self,
        task_id: str,
        *,
        pipeline_mode: PipelineMode = PipelineMode.FULL,
        stage: NodeName = "ready",
    ) -> TaskState:
        """Create a fresh ``TaskState`` row for a task entering the machine.

        No-op if the row already exists (returns the loaded state).
        """
        try:
            return self.load(task_id)
        except TaskNotFound:
            pass
        state = TaskState(
            task_id=task_id,
            stage=stage,
            pipeline_mode=pipeline_mode,
            limits=self.limits,
        )
        self.save(state)
        return state
