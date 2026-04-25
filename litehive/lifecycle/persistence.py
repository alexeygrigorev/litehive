import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import PipelineState, canonical_pipeline_state, utcnow
from litehive.domain.recovery import RecoveryOutcome, RecoveryTrigger
from litehive.tasks.event_log import append_task_event

from .types import FailedReason, NodeName, PipelineMode


@dataclass(frozen=True)
class Limits:
    stage_retry_limit: int = 3
    same_hook_reject_limit: int = 3
    rejection_loop_limit: int = 3
    same_engine_retry_limit: int = 3
    overall_retry_limit: int = 30
    grace_period_seconds: int = 120


@dataclass
class LastReport:
    files_changed: int = 0
    tests_added: int = 0
    changed_files: list[str] = field(default_factory=list)
    test_results: list[str] = field(default_factory=list)
    hook_ok: bool = False

    def to_payload(self) -> dict[str, int | list[str] | bool]:
        return {
            "files_changed": self.files_changed,
            "tests_added": self.tests_added,
            "changed_files": list(self.changed_files),
            "test_results": list(self.test_results),
            "hook_ok": self.hook_ok,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LastReport":
        files_changed = payload.get("files_changed", 0)
        return cls(
            files_changed=len(files_changed) if isinstance(files_changed, list) else int(files_changed),
            tests_added=int(payload.get("tests_added", 0)),
            changed_files=_string_list(payload.get("changed_files")),
            test_results=_string_list(payload.get("test_results")),
            hook_ok=bool(payload.get("hook_ok", False)),
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
            point=canonical_pipeline_state(payload["point"]),
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
    classification: str | None = None

    def to_payload(self) -> dict[str, str | None]:
        payload: dict[str, str | None] = {
            "source": self.source,
            "reason": self.reason,
            "raised_at_phase": self.raised_at_phase,
        }
        if self.classification is not None:
            payload["classification"] = self.classification
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LastRejection":
        return cls(
            source=payload["source"],
            reason=payload["reason"],
            raised_at_phase=canonical_pipeline_state(payload["raised_at_phase"]),
            classification=payload.get("classification"),
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
class RejectionLoop:
    rejection_stage: NodeName
    retry_target_stage: NodeName
    count: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "rejection_stage": self.rejection_stage,
            "retry_target_stage": self.retry_target_stage,
            "count": self.count,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RejectionLoop":
        return cls(
            rejection_stage=payload["rejection_stage"],
            retry_target_stage=payload["retry_target_stage"],
            count=int(payload.get("count") or 0),
        )


@dataclass
class FailedRunRecord:
    """Persistent summary of a terminal failed run shape.

    Records are keyed by stage plus a normalized failure shape. They are
    mirrored into TaskRuntime so requeue/reset paths cannot erase the fact
    that a task repeatedly exhausted the same stage retry budget.
    """

    stage: NodeName
    failure_shape: str
    count: int = 0
    first_at: str | None = None
    latest_at: str | None = None
    last_reason: str = ""
    source: str | None = None
    classification: str | None = None
    retry_limit: int | None = None
    failed_reason: str | None = None
    operator_override_count: int = 0
    last_operator_override_at: str | None = None

    @property
    def key(self) -> str:
        return failed_run_key(self.stage, self.failure_shape)

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "failure_shape": self.failure_shape,
            "count": self.count,
            "first_at": self.first_at,
            "latest_at": self.latest_at,
            "last_reason": self.last_reason,
            "source": self.source,
            "classification": self.classification,
            "retry_limit": self.retry_limit,
            "failed_reason": self.failed_reason,
            "operator_override_count": self.operator_override_count,
            "last_operator_override_at": self.last_operator_override_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FailedRunRecord":
        retry_limit = payload.get("retry_limit")
        retry_limit_value = None if retry_limit in (None, "") else int(retry_limit)
        return cls(
            stage=str(payload.get("stage") or ""),
            failure_shape=str(payload.get("failure_shape") or ""),
            count=int(payload.get("count") or 0),
            first_at=payload.get("first_at"),
            latest_at=payload.get("latest_at"),
            last_reason=str(payload.get("last_reason") or ""),
            source=payload.get("source"),
            classification=payload.get("classification"),
            retry_limit=retry_limit_value,
            failed_reason=payload.get("failed_reason"),
            operator_override_count=int(payload.get("operator_override_count") or 0),
            last_operator_override_at=payload.get("last_operator_override_at"),
        )


def failed_run_key(stage: str, failure_shape: str) -> str:
    return f"{stage}:{failure_shape}"


@dataclass
class TaskState:
    """Single source of truth for task state the machine reads and writes.

    OWNERSHIP PATHS:

    TaskState is owned by the pipeline state machine and Runner:
    - Guards, rule targets, and effect factories receive TaskState read-only
    - Runner is the ONLY component that mutates TaskState via StateDelta patches
    - Pipeline transition rules determine valid state changes
    - Recovery logic reads/writes recovery-specific fields

    DIVERGENCE FROM TASKRUNTIME:

    TaskState and TaskRuntime serve different purposes and diverge in scope:

    TaskState (this class) tracks:
    - High-level pipeline position (stage, pipeline_mode)
    - Retry counts and recovery state (stage_retry, active_recovery_trigger,
      recovery_history, failed_run_history)
    - Terminal failure state (failed_reason, failed_message)
    - Merge and commit state (merge_context, commit_result)
    - Core pipeline state machine data

    TaskRuntime tracks:
    - PipelineRuntime state (run status, stage progress, retries, outcomes)
    - ExecutionRuntime state (subagents, continuations, interruptions)
    - Runtime-only data that may not persist across restarts

    WHEN THEY DIVERGE:

    - TaskState persists across task restarts and represents the canonical
      machine state that the pipeline runner must respect
    - TaskRuntime may be reconstructed or reset during task restarts,
      losing detailed execution context while preserving core pipeline position
    - TaskState drives pipeline routing decisions; TaskRuntime provides
      execution context and debugging information

    PERSISTENCE NOTES:

    ``limits`` is runtime config (not persisted) — real persistence adapters
    should omit it on save and re-inject it on load.

    RECOVERY VOCABULARY:

    ``active_recovery_trigger`` stores the current ``RecoveryTrigger``: the
    structured cause/context for the recovery turn. ``recovery_history`` stores
    completed ``RecoveryOutcome`` objects. ``failed_run_history`` is separate
    cross-run retry-exhaustion memory and is not a recovery outcome.
    """

    task_id: str
    stage: NodeName
    pipeline_mode: PipelineMode
    entry_stage: NodeName | None = None
    stage_retry: dict[NodeName, int] = field(default_factory=dict)
    active_recovery_trigger: RecoveryTrigger | None = None
    recovery_history: list[RecoveryOutcome] = field(default_factory=list)
    pre_exec_recovery_attempt: int = 0
    agent_elapsed_seconds: float = 0.0
    merge_context: MergeContext | None = None
    commit_result: CommitResult | None = None
    last_report: LastReport = field(default_factory=LastReport)
    last_rejection_by_stage: dict[NodeName, LastRejection] = field(default_factory=dict)
    failed_run_history: dict[str, FailedRunRecord] = field(default_factory=dict)
    rejection_loop: RejectionLoop | None = None
    consecutive_same_hook_rejects: int = 0
    last_hook_reject_fingerprint: HookRejectFingerprint | None = None
    hook_reject_recovery_invoked: bool = False
    failed_reason: FailedReason | None = None
    failed_message: str | None = None
    recovery_failure_explanation: str | None = None
    limits: Limits = field(default_factory=Limits)

    def __post_init__(self) -> None:
        self.stage = canonical_pipeline_state(self.stage)
        if self.entry_stage is not None:
            self.entry_stage = canonical_pipeline_state(self.entry_stage)
        self.stage_retry = {
            canonical_pipeline_state(stage): retry_count for stage, retry_count in self.stage_retry.items()
        }
        self.last_rejection_by_stage = {
            canonical_pipeline_state(stage): rejection for stage, rejection in self.last_rejection_by_stage.items()
        }

    def recovery_attempts_for_origin(self, origin_stage: NodeName) -> int:
        count = sum(1 for outcome in self.recovery_history if outcome.trigger.origin_stage == origin_stage)
        if self.active_recovery_trigger is not None and self.active_recovery_trigger.origin_stage == origin_stage:
            count += 1
        return count

    def recovery_budget_available(self, trigger: RecoveryTrigger) -> bool:
        if trigger.reason_code == "hook_reject_loop":
            return not self.hook_reject_recovery_invoked
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


# ── sqlite-backed persistence ────────────────────────────────────────────


def _state_payload(state: TaskState) -> dict[str, Any]:
    return {
        "entry_stage": None if state.entry_stage is None else str(state.entry_stage),
        "stage_retry": {str(stage): count for stage, count in state.stage_retry.items()},
        "active_recovery_trigger": (
            state.active_recovery_trigger.to_payload() if state.active_recovery_trigger is not None else None
        ),
        "recovery_history": [outcome.to_payload() for outcome in state.recovery_history],
        "pre_exec_recovery_attempt": state.pre_exec_recovery_attempt,
        "agent_elapsed_seconds": state.agent_elapsed_seconds,
        "merge_context": (state.merge_context.to_payload() if state.merge_context is not None else None),
        "commit_result": (state.commit_result.to_payload() if state.commit_result is not None else None),
        "last_report": state.last_report.to_payload(),
        "last_rejection_by_stage": {str(stage): rej.to_payload() for stage, rej in state.last_rejection_by_stage.items()},
        "failed_run_history": {key: record.to_payload() for key, record in state.failed_run_history.items()},
        "rejection_loop": (state.rejection_loop.to_payload() if state.rejection_loop is not None else None),
        "consecutive_same_hook_rejects": state.consecutive_same_hook_rejects,
        "last_hook_reject_fingerprint": (
            state.last_hook_reject_fingerprint.to_payload() if state.last_hook_reject_fingerprint is not None else None
        ),
        "hook_reject_recovery_invoked": state.hook_reject_recovery_invoked,
        "failed_reason": state.failed_reason,
        "failed_message": state.failed_message,
        "recovery_failure_explanation": state.recovery_failure_explanation,
    }


def _state_from_row(
    task_id: str,
    stage: str | PipelineState,
    pipeline_mode: str,
    payload: dict[str, Any],
    limits: Limits,
) -> TaskState:
    last_report_data = payload.get("last_report") or {}
    last_rejections_data = payload.get("last_rejection_by_stage") or {}
    failed_run_history_data = payload.get("failed_run_history") or {}
    hook_fingerprint_data = payload.get("last_hook_reject_fingerprint") or None
    active_recovery_trigger = payload.get("active_recovery_trigger")
    recovery_history = payload.get("recovery_history")
    merge_context = payload.get("merge_context")
    commit_result = payload.get("commit_result")
    rejection_loop = payload.get("rejection_loop")
    return TaskState(
        task_id=task_id,
        stage=canonical_pipeline_state(stage),
        pipeline_mode=PipelineMode(pipeline_mode),
        entry_stage=(None if payload.get("entry_stage") is None else canonical_pipeline_state(payload["entry_stage"])),
        stage_retry={
            canonical_pipeline_state(stage_name): int(retry_count)
            for stage_name, retry_count in (payload.get("stage_retry") or {}).items()
        },
        active_recovery_trigger=(
            RecoveryTrigger.from_payload(dict(active_recovery_trigger))
            if isinstance(active_recovery_trigger, dict)
            else None
        ),
        recovery_history=[
            RecoveryOutcome.from_payload(dict(item)) for item in list(recovery_history or []) if isinstance(item, dict)
        ],
        pre_exec_recovery_attempt=int(payload.get("pre_exec_recovery_attempt") or 0),
        agent_elapsed_seconds=float(payload.get("agent_elapsed_seconds") or 0.0),
        merge_context=(MergeContext.from_payload(dict(merge_context)) if isinstance(merge_context, dict) else None),
        commit_result=(CommitResult.from_payload(dict(commit_result)) if isinstance(commit_result, dict) else None),
        last_report=LastReport.from_payload(last_report_data),
        last_rejection_by_stage={
            canonical_pipeline_state(stage_name): LastRejection.from_payload(rej)
            for stage_name, rej in last_rejections_data.items()
        },
        failed_run_history={
            str(key): FailedRunRecord.from_payload(dict(record))
            for key, record in failed_run_history_data.items()
            if isinstance(record, dict)
        },
        rejection_loop=(RejectionLoop.from_payload(dict(rejection_loop)) if isinstance(rejection_loop, dict) else None),
        consecutive_same_hook_rejects=int(payload.get("consecutive_same_hook_rejects") or 0),
        last_hook_reject_fingerprint=(
            HookRejectFingerprint.from_payload(hook_fingerprint_data) if hook_fingerprint_data is not None else None
        ),
        hook_reject_recovery_invoked=bool(payload.get("hook_reject_recovery_invoked", False)),
        failed_reason=(FailedReason(payload["failed_reason"]) if payload.get("failed_reason") else None),
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
        payload = _state_payload(state)
        payload_json = json.dumps(payload, sort_keys=True)
        updated_at = utcnow()
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
                    str(state.stage),
                    state.pipeline_mode.value,
                    payload_json,
                    updated_at,
                ),
            )
            append_task_event(
                self.workspace_root,
                event_type="pipeline_task_state_saved",
                task_id=state.task_id,
                payload={
                    "pipeline_task_state": {
                        "task_id": state.task_id,
                        "stage": str(state.stage),
                        "pipeline_mode": state.pipeline_mode.value,
                        "payload": payload,
                        "updated_at": updated_at,
                    }
                },
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
            append_task_event(
                self.workspace_root,
                event_type="pipeline_task_state_reset",
                task_id=task_id,
                payload={},
            )
            connection.commit()

    def initialize(
        self,
        task_id: str,
        *,
        pipeline_mode: PipelineMode = PipelineMode.FULL,
        stage: NodeName = PipelineState.READY,
        entry_stage: NodeName | None = None,
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
            entry_stage=entry_stage,
            limits=self.limits,
        )
        self.save(state)
        return state
