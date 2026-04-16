import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.domain.recovery import RecoveryOutcome, RecoveryTrigger

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

    def to_payload(self) -> dict[str, int]:
        return {
            "files_changed": self.files_changed,
            "tests_added": self.tests_added,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LastReport":
        return cls(
            files_changed=int(payload.get("files_changed", 0)),
            tests_added=int(payload.get("tests_added", 0)),
        )


@dataclass
class HookRejectFingerprint:
    point: NodeName
    command: str
    description: str = ""
    fingerprint: str = ""

    def to_payload(self) -> dict[str, str]:
        return {
            "point": self.point,
            "command": self.command,
            "description": self.description,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HookRejectFingerprint":
        return cls(
            point=payload["point"],
            command=payload["command"],
            description=payload.get("description", ""),
            fingerprint=payload["fingerprint"],
        )


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

    def to_payload(self) -> dict[str, str]:
        return {
            "source": self.source,
            "reason": self.reason,
            "raised_at_phase": self.raised_at_phase,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LastRejection":
        return cls(
            source=payload["source"],
            reason=payload["reason"],
            raised_at_phase=payload["raised_at_phase"],
        )


@dataclass
class MergeContext:
    conflict_files: tuple[str, ...] = ()
    merge_attempt: int = 1

    def to_payload(self) -> dict[str, Any]:
        return {
            "conflict_files": list(self.conflict_files),
            "merge_attempt": self.merge_attempt,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MergeContext":
        files = payload.get("conflict_files") or []
        return cls(
            conflict_files=tuple(str(path) for path in files),
            merge_attempt=int(payload.get("merge_attempt") or 1),
        )


@dataclass
class CommitResult:
    head_sha: str
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "head_sha": self.head_sha,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CommitResult":
        return cls(
            head_sha=str(payload["head_sha"]),
            reason=payload.get("reason"),
        )


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
    active_recovery_trigger: RecoveryTrigger | None = None
    recovery_history: list[RecoveryOutcome] = field(default_factory=list)
    pre_exec_recovery_attempt: int = 0
    merge_context: MergeContext | None = None
    commit_result: CommitResult | None = None
    last_report: LastReport = field(default_factory=LastReport)
    last_rejection_by_stage: dict[NodeName, LastRejection] = field(default_factory=dict)
    consecutive_same_hook_rejects: int = 0
    last_hook_reject_fingerprint: HookRejectFingerprint | None = None
    hook_reject_recovery_invoked: bool = False
    failed_reason: FailedReason | None = None
    failed_message: str | None = None
    recovery_failure_explanation: str | None = None
    limits: Limits = field(default_factory=Limits)

    def recovery_attempts_for_origin(self, origin_stage: NodeName) -> int:
        count = sum(1 for outcome in self.recovery_history if outcome.trigger.origin_stage == origin_stage)
        if self.active_recovery_trigger is not None and self.active_recovery_trigger.origin_stage == origin_stage:
            count += 1
        return count

    def recovery_budget_available(self, trigger: RecoveryTrigger) -> bool:
        return all(outcome.trigger.budget_key() != trigger.budget_key() for outcome in self.recovery_history)


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
        "active_recovery_trigger": (
            state.active_recovery_trigger.to_payload()
            if state.active_recovery_trigger is not None
            else None
        ),
        "recovery_history": [outcome.to_payload() for outcome in state.recovery_history],
        "pre_exec_recovery_attempt": state.pre_exec_recovery_attempt,
        "merge_context": (
            state.merge_context.to_payload()
            if state.merge_context is not None
            else None
        ),
        "commit_result": (
            state.commit_result.to_payload()
            if state.commit_result is not None
            else None
        ),
        "last_report": state.last_report.to_payload(),
        "last_rejection_by_stage": {
            stage: rej.to_payload()
            for stage, rej in state.last_rejection_by_stage.items()
        },
        "consecutive_same_hook_rejects": state.consecutive_same_hook_rejects,
        "last_hook_reject_fingerprint": (
            state.last_hook_reject_fingerprint.to_payload()
            if state.last_hook_reject_fingerprint is not None
            else None
        ),
        "hook_reject_recovery_invoked": state.hook_reject_recovery_invoked,
        "failed_reason": state.failed_reason,
        "failed_message": state.failed_message,
        "recovery_failure_explanation": state.recovery_failure_explanation,
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
    active_recovery_trigger = payload.get("active_recovery_trigger")
    recovery_history = payload.get("recovery_history")
    merge_context = payload.get("merge_context")
    commit_result = payload.get("commit_result")
    return TaskState(
        task_id=task_id,
        stage=stage,
        pipeline_mode=PipelineMode(pipeline_mode),
        stage_retry=dict(payload.get("stage_retry") or {}),
        active_recovery_trigger=(
            RecoveryTrigger.from_payload(dict(active_recovery_trigger))
            if isinstance(active_recovery_trigger, dict)
            else None
        ),
        recovery_history=[
            RecoveryOutcome.from_payload(dict(item))
            for item in list(recovery_history or [])
            if isinstance(item, dict)
        ],
        pre_exec_recovery_attempt=int(payload.get("pre_exec_recovery_attempt") or 0),
        merge_context=(
            MergeContext.from_payload(dict(merge_context))
            if isinstance(merge_context, dict)
            else None
        ),
        commit_result=(
            CommitResult.from_payload(dict(commit_result))
            if isinstance(commit_result, dict)
            else None
        ),
        last_report=LastReport.from_payload(last_report_data),
        last_rejection_by_stage={
            stage_name: LastRejection.from_payload(rej)
            for stage_name, rej in last_rejections_data.items()
        },
        consecutive_same_hook_rejects=int(payload.get("consecutive_same_hook_rejects") or 0),
        last_hook_reject_fingerprint=(
            HookRejectFingerprint.from_payload(hook_fingerprint_data)
            if hook_fingerprint_data is not None
            else None
        ),
        hook_reject_recovery_invoked=bool(payload.get("hook_reject_recovery_invoked", False)),
        failed_reason=(
            FailedReason(payload["failed_reason"])
            if payload.get("failed_reason")
            else None
        ),
        failed_message=payload.get("failed_message"),
        recovery_failure_explanation=payload.get("recovery_failure_explanation"),
        limits=limits,
    )


class TaskNotFound(LookupError):
    """Raised when ``SqlitePersistence.load`` is called on an unknown task id."""


class SqlitePersistence:
    """Persists ``TaskState`` to the ``pipeline_task_state`` sqlite table.

    The scalar fields (stage, pipeline_mode) are stored as columns so the
    daemon can query them directly without parsing JSON. Everything else
    (counters, recovery/merge details, last_rejection_by_stage, last_report, failed_*)
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

    def reset(self, task_id: str) -> None:
        """Delete the v2 pipeline state row for a task.

        Called when the task-level layer resets a flagged task back to
        queued (dequeue auto-recovery path). Without this, the v2 state
        machine keeps its terminal ``failed`` stage and the runner
        immediately re-emits the failed terminal on the next run — an
        infinite retry loop.
        """
        with connect_workspace_db(self.workspace_root) as connection:
            connection.execute("DELETE FROM pipeline_task_state WHERE task_id = ?", (task_id,))
            connection.commit()

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
