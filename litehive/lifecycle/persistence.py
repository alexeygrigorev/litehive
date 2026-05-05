import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from litehive.domain.common import PipelineState, canonical_pipeline_state, utcnow
from litehive.domain.recovery import RecoveryOutcome, RecoveryTrigger
from litehive.tasks.event_log import append_task_event
from litehive.workspace import Workspace

from .types import FailedReason, PipelineMode


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
        """Serialize the SWE/agent report into the json payload column so the next runner launch can read it back."""
        return {
            "files_changed": self.files_changed,
            "tests_added": self.tests_added,
            "changed_files": list(self.changed_files),
            "test_results": list(self.test_results),
            "hook_ok": self.hook_ok,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LastReport":
        """Rebuild a report from the persisted json payload, tolerating old shapes where ``files_changed`` was stored as a list rather than a count."""
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
    point: PipelineState
    command: str
    description: str = ""
    fingerprint: str = ""

    def to_payload(self) -> dict[str, str]:
        """Serialize the hook fingerprint so the same-hook-reject circuit breaker survives across runner launches."""
        return {
            "point": self.point,
            "command": self.command,
            "description": self.description,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HookRejectFingerprint":
        """Rebuild a hook-reject fingerprint from json so the next launch can compare against the latest hook reject and detect repeats."""
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
    raised_at_phase: PipelineState  # the phase that emitted the reject
    classification: str | None = None

    def to_payload(self) -> dict[str, str | None]:
        """Serialize the rejection so the next agent visit's prompt builder can recall what the previous attempt was rejected for."""
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
        """Rebuild the per-stage rejection memo from json; read by ``RoleAgent.build_prompt`` on retry."""
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
        """Serialize merge state so a runner that resumes mid-merge knows the conflict files and which attempt it is on."""
        return {
            "conflict_files": list(self.conflict_files),
            "merge_attempt": self.merge_attempt,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MergeContext":
        """Rebuild merge state from json; relied on by the merge stage to resume an interrupted conflict resolution."""
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
        """Serialize the commit-stage outcome (head sha and skip reason) so downstream stages can reference what the commit step produced."""
        return {
            "head_sha": self.head_sha,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CommitResult":
        """Rebuild the commit-stage outcome from json so a resumed runner sees the committed worktree head sha."""
        return cls(
            head_sha=str(payload["head_sha"]),
            reason=payload.get("reason"),
        )


@dataclass
class RejectionLoop:
    rejection_stage: PipelineState
    retry_target_stage: PipelineState
    count: int = 0

    def to_payload(self) -> dict[str, Any]:
        """Serialize loop counters so the rejection-loop cap survives across runs and can fail the task once the same reject keeps bouncing back."""
        return {
            "rejection_stage": self.rejection_stage,
            "retry_target_stage": self.retry_target_stage,
            "count": self.count,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RejectionLoop":
        """Rebuild the rejection-loop counter from json so the next reject knows whether the cap is already reached."""
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

    stage: PipelineState
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
        """Stable lookup key for this record inside ``failed_run_history``; used by the runner's retry-exhaustion bookkeeping to find or upsert the matching row."""
        return failed_run_key(self.stage, self.failure_shape)

    def to_payload(self) -> dict[str, Any]:
        """Serialize the failed-run record so cross-run retry-exhaustion memory survives a task restart and is mirrored into TaskRuntime."""
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
        """Rebuild a failed-run record from json so the runner can refuse to re-enter a stage that has already exhausted its retry budget for this failure shape."""
        retry_limit = payload.get("retry_limit")
        if retry_limit in (None, ""):
            retry_limit_value = None
        else:
            retry_limit_value = int(retry_limit)
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
    """Canonical ``failed_run_history`` key shared between persistence and ``litehive.tasks.failed_runs`` so both sides agree how stage + failure shape compose into one slot."""
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
    stage: PipelineState
    pipeline_mode: PipelineMode
    entry_stage: PipelineState | None = None
    stage_retry: dict[PipelineState, int] = field(default_factory=dict)
    active_recovery_trigger: RecoveryTrigger | None = None
    recovery_history: list[RecoveryOutcome] = field(default_factory=list)
    recovery_budget_history_start: int = 0
    pre_exec_recovery_attempt: int = 0
    agent_elapsed_seconds: float = 0.0
    merge_context: MergeContext | None = None
    commit_result: CommitResult | None = None
    last_report: LastReport = field(default_factory=LastReport)
    last_rejection_by_stage: dict[PipelineState, LastRejection] = field(default_factory=dict)
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
        """Canonicalize all PipelineState fields right after construction so guards/effects/persistence comparing stage names by equality never see a raw string side-by-side with an enum member."""
        self.stage = canonical_pipeline_state(self.stage)
        if self.entry_stage is not None:
            self.entry_stage = canonical_pipeline_state(self.entry_stage)
        self.stage_retry = {
            canonical_pipeline_state(stage): retry_count for stage, retry_count in self.stage_retry.items()
        }
        self.last_rejection_by_stage = {
            canonical_pipeline_state(stage): rejection for stage, rejection in self.last_rejection_by_stage.items()
        }

    def recovery_attempts_for_origin(self, origin_stage: PipelineState) -> int:
        """Count completed plus in-flight recovery attempts that originated at ``origin_stage``; the recovery-budget guard uses this to decide whether the origin stage has already used its allotment."""
        count = sum(1 for outcome in self._budget_recovery_history() if outcome.trigger.origin_stage == origin_stage)
        if self.active_recovery_trigger is not None and self.active_recovery_trigger.origin_stage == origin_stage:
            count += 1
        return count

    def recovery_budget_available(self, trigger: RecoveryTrigger) -> bool:
        """Decide whether the lifecycle rules may dispatch another recovery turn for this trigger; consulted by the ``recovery_budget_available`` guard before routing a Reject into recovery."""
        if trigger.reason_code == "hook_reject_loop":
            return not self.hook_reject_recovery_invoked
        return all(outcome.trigger.budget_key() != trigger.budget_key() for outcome in self._budget_recovery_history())

    def _budget_recovery_history(self) -> list[RecoveryOutcome]:
        """Slice of ``recovery_history`` that counts toward the current budget window; pre-recovery outcomes preserved across a budget reset (e.g. after a successful operator override) are excluded so they cannot exhaust the new window."""
        return self.recovery_history[max(0, self.recovery_budget_history_start) :]


class Persistence(Protocol):
    """Structural type the runner depends on so tests can substitute an in-memory persistence without inheriting from ``SqlitePersistence``."""

    def save(self, state: TaskState) -> None:
        """Persist the lifecycle cursor for one task; tests may swap a no-op implementation, the production binding writes ``pipeline_task_state``."""
        ...

    def load(self, task_id: str) -> TaskState:
        """Return the persisted cursor for a task; tests may return an in-memory record, the production binding reads ``pipeline_task_state`` and raises ``TaskNotFound`` for unknown ids."""
        ...


def _string_list(value: Any) -> list[str]:
    """Coerce a json field into a deduped, stripped list of non-empty strings; used by ``LastReport.from_payload`` to defend report-rebuild against payloads with stray empty entries or non-string items."""
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
    """Serialize the non-column fields of ``TaskState`` into the json blob ``SqlitePersistence.save`` writes to ``pipeline_task_state.payload``; the inverse of ``_state_from_row`` and the only producer of that column shape."""
    return {
        "entry_stage": None if state.entry_stage is None else str(state.entry_stage),
        "stage_retry": {str(stage): count for stage, count in state.stage_retry.items()},
        "active_recovery_trigger": (
            state.active_recovery_trigger.to_payload() if state.active_recovery_trigger is not None else None
        ),
        "recovery_history": [outcome.to_payload() for outcome in state.recovery_history],
        "recovery_budget_history_start": state.recovery_budget_history_start,
        "pre_exec_recovery_attempt": state.pre_exec_recovery_attempt,
        "agent_elapsed_seconds": state.agent_elapsed_seconds,
        "merge_context": (state.merge_context.to_payload() if state.merge_context is not None else None),
        "commit_result": (state.commit_result.to_payload() if state.commit_result is not None else None),
        "last_report": state.last_report.to_payload(),
        "last_rejection_by_stage": {
            str(stage): rej.to_payload() for stage, rej in state.last_rejection_by_stage.items()
        },
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
    """Rebuild a ``TaskState`` from one ``pipeline_task_state`` row; the inverse of ``_state_payload``, called by ``SqlitePersistence.load`` so the runner can resume from where the previous launch left off."""
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
        recovery_budget_history_start=int(payload.get("recovery_budget_history_start") or 0),
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
    """Persists the current lifecycle cursor to ``pipeline_task_state``.

    The scalar fields (stage, pipeline_mode) are stored as columns so the
    daemon can query them directly without parsing JSON. Everything else
    (counters, recovery/merge details, last_rejection_by_stage, last_report, failed_*)
    lives in the free-form ``payload`` column.

    Boundary for metrics and history:
    - main-stage attempt counts come from append-only ``pipeline_transitions``;
    - operator/status/recovery mutations come from task audit/events;
    - engine continuation data comes from ``pipeline_sessions``.

    ``pipeline_task_state`` is only the mutable current-state cursor that the
    runner resumes from. It is not a transition history or audit source.

    ``limits`` is runtime config and is re-injected from the ``limits``
    constructor argument on every load — it never hits the db.
    """

    def __init__(self, workspace: Workspace, limits: Limits | None = None) -> None:
        """Bind the persistence to a workspace plus a (re-injected on every load) ``Limits`` config; constructed once per runner launch so every save/load against this task uses the same retry budget configuration."""
        self.workspace = workspace
        self.limits = limits or Limits()

    def save(self, state: TaskState) -> None:
        """Upsert the lifecycle cursor for this task and append a ``pipeline_task_state_saved`` audit event; called by the runner after every state-machine step that mutates ``TaskState``."""
        payload = _state_payload(state)
        payload_json = json.dumps(payload, sort_keys=True)
        updated_at = utcnow()
        with self.workspace.connect() as connection:
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
                self.workspace,
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
        """Return the last persisted ``TaskState`` for the task; raises ``TaskNotFound`` for tasks that never reached the runner. Called by the runner on launch and by status/debug/diagnostics readers."""
        with self.workspace.connect() as connection:
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

    def reset_current_lifecycle_state(self, task_id: str, preserve_run_memory: bool = False) -> None:
        """Reset the current lifecycle cursor for a task.

        This deletes or rewrites only the mutable ``pipeline_task_state`` row
        that the runner resumes from. Append-only ``pipeline_transitions`` and
        ``pipeline_journal`` history are preserved; ``pipeline_sessions`` rows
        are also left intact because they store engine continuation data.

        Called when the task-level layer resets a flagged task back to queued
        (dequeue auto-recovery path), or closes/cancels a task. Requeue callers
        can preserve lifecycle-owned failed-run/recovery memory so the next
        runner launch does not need to copy it back from runtime.
        """
        try:
            previous = self.load(task_id)
        except TaskNotFound:
            previous = None
        with self.workspace.connect() as connection:
            if previous is None:
                preserved_failed_runs = {}
            else:
                preserved_failed_runs = previous.failed_run_history
            if previous is None:
                preserved_recovery_history = []
            else:
                preserved_recovery_history = previous.recovery_history
            if (
                previous is None
                or not preserve_run_memory
                or (not preserved_failed_runs and not preserved_recovery_history)
            ):
                connection.execute("DELETE FROM pipeline_task_state WHERE task_id = ?", (task_id,))
            else:
                reset_state = TaskState(
                    task_id=task_id,
                    stage=PipelineState.READY,
                    pipeline_mode=previous.pipeline_mode,
                    recovery_history=previous.recovery_history,
                    recovery_budget_history_start=len(previous.recovery_history),
                    failed_run_history=preserved_failed_runs,
                    limits=self.limits,
                )
                payload = _state_payload(reset_state)
                connection.execute(
                    """
                    UPDATE pipeline_task_state
                    SET stage = ?, pipeline_mode = ?, payload = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        str(reset_state.stage),
                        reset_state.pipeline_mode.value,
                        json.dumps(payload, sort_keys=True),
                        utcnow(),
                        task_id,
                    ),
                )
            append_task_event(
                self.workspace,
                event_type="pipeline_task_state_reset",
                task_id=task_id,
                payload={},
            )
            connection.commit()

    def reset_all(self, task_id: str) -> None:
        """Remove all lifecycle-owned runtime rows for a task."""
        with self.workspace.connect() as connection:
            for table_name in ("pipeline_task_state", "pipeline_sessions", "pipeline_transitions", "pipeline_journal"):
                connection.execute(f"DELETE FROM {table_name} WHERE task_id = ?", (task_id,))
            append_task_event(
                self.workspace,
                event_type="pipeline_task_state_reset",
                task_id=task_id,
                payload={},
            )
            connection.commit()

    def initialize(
        self,
        task_id: str,
        pipeline_mode: PipelineMode = PipelineMode.FULL,
        stage: PipelineState = PipelineState.READY,
        entry_stage: PipelineState | None = None,
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
