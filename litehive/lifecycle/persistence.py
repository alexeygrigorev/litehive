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
        """Serialize the report so the next runner launch can read it back from the json column."""
        return {
            "files_changed": self.files_changed,
            "tests_added": self.tests_added,
            "changed_files": list(self.changed_files),
            "test_results": list(self.test_results),
            "hook_ok": self.hook_ok,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LastReport":
        """
        Rebuild a report from the persisted json payload.

        Tolerates legacy shapes where ``files_changed`` was stored as a
        list of paths rather than an integer count, so a workspace
        upgraded across that schema change does not lose its in-flight
        report on first reload.
        """
        files_changed = payload.get("files_changed", 0)
        if isinstance(files_changed, list):
            files_changed_count = len(files_changed)
        else:
            files_changed_count = int(files_changed)
        return cls(
            files_changed=files_changed_count,
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
        """
        Serialize the hook fingerprint to json.

        The same-hook-reject circuit breaker compares fingerprints
        across launches, so persisting this is what lets the breaker
        survive a runner restart instead of resetting its streak count
        on every process boot.
        """
        return {
            "point": self.point,
            "command": self.command,
            "description": self.description,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HookRejectFingerprint":
        """Rebuild a hook-reject fingerprint from json so the next launch can detect repeats of the latest hook reject."""
        return cls(
            point=canonical_pipeline_state(payload["point"]),
            command=payload["command"],
            description=payload.get("description", ""),
            fingerprint=payload["fingerprint"],
        )


@dataclass
class LastRejection:
    """Most recent reject against a retry-eligible stage.

    Populated by the ``IncStageRetry`` effect whenever a Reject event is
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
        """Rebuild the per-stage rejection memo from json so ``RoleAgent.build_prompt`` can render it on retry."""
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
        """
        Serialize merge state to json.

        A runner that resumes mid-merge needs to know which files were
        conflicting and which attempt it is on so it does not start the
        retry counter from zero or forget which files to re-resolve.
        """
        return {
            "conflict_files": list(self.conflict_files),
            "merge_attempt": self.merge_attempt,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MergeContext":
        """Rebuild merge state from json so the merge stage can resume an interrupted conflict resolution."""
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
        """Serialize the commit-stage outcome (head sha plus optional skip reason) so downstream stages can reference what landed."""
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
        """
        Serialize the rejection-loop counter to json.

        Persisting this is what lets the rejection-loop cap survive a
        runner restart so a task that has been bouncing the same reject
        back and forth does not get a fresh retry budget on every
        relaunch.
        """
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
        """Stable lookup key inside ``failed_run_history`` so the runner's retry-exhaustion bookkeeping can find or upsert this row."""
        return failed_run_key(self.stage, self.failure_shape)

    def to_payload(self) -> dict[str, Any]:
        """
        Serialize the failed-run record to json.

        Cross-run retry-exhaustion memory is stored on the runtime
        TaskRecord *and* in lifecycle ``TaskState`` so a requeue cannot
        wipe it; this method produces the shape both stores agree on.
        """
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
        """
        Rebuild a failed-run record from json.

        Without this, a relaunched runner would re-enter a stage whose
        retry budget had already been exhausted for this exact failure
        shape; the rebuild is what enforces "do not retry the same
        failure forever".
        """
        retry_limit = payload.get("retry_limit")
        if retry_limit in (None, ""):
            retry_limit_value = None
        else:
            retry_limit_value = int(retry_limit)
        return cls(
            stage=PipelineState(str(payload.get("stage") or "")),
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
    """
    Canonical key under which a failed-run record lives.

    Shared between persistence and ``litehive.tasks.failed_runs`` so
    both sides agree how stage + failure shape compose into one slot;
    if the two sides ever disagreed, runtime and lifecycle would carry
    parallel records of the same failure under different keys.
    """
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
        """
        Canonicalize all PipelineState fields right after construction.

        Guards, effects, and persistence compare stage names by equality;
        without this, a stage loaded from json as a raw string could sit
        side-by-side with an enum-typed stage from a freshly-built state
        and equality checks would silently miss.
        """
        self.stage = canonical_pipeline_state(self.stage)
        if self.entry_stage is not None:
            self.entry_stage = canonical_pipeline_state(self.entry_stage)
        self.stage_retry = {
            canonical_pipeline_state(stage): retry_count for stage, retry_count in self.stage_retry.items()
        }
        self.last_rejection_by_stage = {
            canonical_pipeline_state(stage): rejection for stage, rejection in self.last_rejection_by_stage.items()
        }

    def recovery_budget_available(self, trigger: RecoveryTrigger) -> bool:
        """
        Decide whether the lifecycle rules may dispatch another recovery
        turn for this trigger.

        Consulted by the ``recovery_budget_available`` guard before
        routing a Reject into recovery. Hook-reject loops use a single
        global "we already invoked recovery" flag; everything else uses
        per-fingerprint budget keys so distinct failure shapes each get
        their own one-shot recovery attempt.
        """
        if trigger.reason_code == "hook_reject_loop":
            return not self.hook_reject_recovery_invoked
        return self._budget_window_unconsumed_for(trigger)

    def _budget_window_unconsumed_for(self, trigger: RecoveryTrigger) -> bool:
        """
        Test whether the current budget window has no recovery for this trigger.

        Linear scan over :meth:`_budget_recovery_history`: if any past
        outcome shares the trigger's budget fingerprint, the slot is
        already spent for this failure shape. Caller:
        :meth:`recovery_budget_available_for`.
        """
        candidate_key = trigger.budget_key()
        for outcome in self._budget_recovery_history():
            if outcome.trigger.budget_key() == candidate_key:
                return False
        return True

    def _budget_recovery_history(self) -> list[RecoveryOutcome]:
        """
        Slice of ``recovery_history`` that counts toward the current
        budget window.

        Pre-recovery outcomes preserved across a budget reset (e.g.
        after a successful operator override that intentionally clears
        the slate) are excluded so they cannot exhaust the new window.
        """
        return self.recovery_history[max(0, self.recovery_budget_history_start) :]


class Persistence(Protocol):
    """
    Structural type the runner depends on for state persistence.

    Tests substitute an in-memory implementation that satisfies the
    protocol without inheriting from ``SqlitePersistence``; production
    uses ``SqlitePersistence`` (the only real implementation).
    """

    def save(self, state: TaskState) -> None:
        """Persist the lifecycle cursor for one task; the production binding writes ``pipeline_task_state`` and tests usually swap a no-op or in-memory implementation."""
        ...

    def load(self, task_id: str) -> TaskState:
        """Return the persisted cursor for a task; production reads ``pipeline_task_state`` and raises ``TaskNotFound`` for unknown ids, tests usually return an in-memory record."""
        ...


def _string_list(value: Any) -> list[str]:
    """
    Coerce a json field into a deduped, stripped list of strings.

    Used by ``LastReport.from_payload`` to defend report-rebuild against
    payloads with stray empty entries or non-string items, so a slightly
    malformed report cannot derail the resume path.
    """
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
    """
    Serialize the non-column fields of ``TaskState`` into the json blob.

    Inverse of ``_state_from_row`` and the only producer of the
    ``pipeline_task_state.payload`` shape; centralising it here is what
    keeps writers and readers from drifting apart on field naming.
    """
    if state.entry_stage is None:
        entry_stage_payload = None
    else:
        entry_stage_payload = state.entry_stage.value
    if state.active_recovery_trigger is not None:
        active_recovery_trigger_payload = state.active_recovery_trigger.to_payload()
    else:
        active_recovery_trigger_payload = None
    if state.merge_context is not None:
        merge_context_payload = state.merge_context.to_payload()
    else:
        merge_context_payload = None
    if state.commit_result is not None:
        commit_result_payload = state.commit_result.to_payload()
    else:
        commit_result_payload = None
    if state.rejection_loop is not None:
        rejection_loop_payload = state.rejection_loop.to_payload()
    else:
        rejection_loop_payload = None
    if state.last_hook_reject_fingerprint is not None:
        last_hook_reject_fingerprint_payload = state.last_hook_reject_fingerprint.to_payload()
    else:
        last_hook_reject_fingerprint_payload = None
    return {
        "entry_stage": entry_stage_payload,
        "stage_retry": {stage.value: count for stage, count in state.stage_retry.items()},
        "active_recovery_trigger": active_recovery_trigger_payload,
        "recovery_history": [outcome.to_payload() for outcome in state.recovery_history],
        "recovery_budget_history_start": state.recovery_budget_history_start,
        "pre_exec_recovery_attempt": state.pre_exec_recovery_attempt,
        "agent_elapsed_seconds": state.agent_elapsed_seconds,
        "merge_context": merge_context_payload,
        "commit_result": commit_result_payload,
        "last_report": state.last_report.to_payload(),
        "last_rejection_by_stage": {
            stage.value: rej.to_payload() for stage, rej in state.last_rejection_by_stage.items()
        },
        "failed_run_history": {key: record.to_payload() for key, record in state.failed_run_history.items()},
        "rejection_loop": rejection_loop_payload,
        "consecutive_same_hook_rejects": state.consecutive_same_hook_rejects,
        "last_hook_reject_fingerprint": last_hook_reject_fingerprint_payload,
        "hook_reject_recovery_invoked": state.hook_reject_recovery_invoked,
        "failed_reason": state.failed_reason,
        "failed_message": state.failed_message,
        "recovery_failure_explanation": state.recovery_failure_explanation,
    }


def _decode_stage_retry_map(raw) -> dict[PipelineState, int]:
    """
    Decode the persisted ``stage_retry`` map for one task.

    Each stage name is canonicalized so legacy spellings round-trip
    onto the current :class:`PipelineState` enum, and retry counts
    are coerced to ``int``. Caller: :func:`_state_from_row`.
    """
    decoded: dict[PipelineState, int] = {}
    if not isinstance(raw, dict):
        return decoded
    for stage_name, retry_count in raw.items():
        decoded[canonical_pipeline_state(stage_name)] = int(retry_count)
    return decoded


def _decode_recovery_history(raw) -> list[RecoveryOutcome]:
    """
    Decode the persisted ``recovery_history`` list of trigger outcomes.

    Skips entries that are not dicts so a malformed row cannot
    crash a task load — the failure here is "recovery audit row
    missing", which the journal still records, vs. losing the
    whole task state. Caller: :func:`_state_from_row`.
    """
    items = list(raw or [])
    decoded: list[RecoveryOutcome] = []
    for item in items:
        if isinstance(item, dict):
            decoded.append(RecoveryOutcome.from_payload(dict(item)))
    return decoded


def _decode_last_rejection_by_stage(
    raw: dict[str, Any],
) -> dict[PipelineState, LastRejection]:
    """
    Decode the per-stage ``last_rejection_by_stage`` map.

    Stage keys are canonicalized so the in-memory map is
    enum-keyed even for legacy persisted spellings. Caller:
    :func:`_state_from_row`.
    """
    decoded: dict[PipelineState, LastRejection] = {}
    for stage_name, rej in raw.items():
        decoded[canonical_pipeline_state(stage_name)] = LastRejection.from_payload(rej)
    return decoded


def _decode_failed_run_history(
    raw: dict[str, Any],
) -> dict[str, FailedRunRecord]:
    """
    Decode the ``failed_run_history`` map keyed by failure shape.

    Drops malformed (non-dict) records the same way
    :func:`_decode_recovery_history` does — a corrupted slot
    must not block reloading the rest of the task state. Caller:
    :func:`_state_from_row`.
    """
    decoded: dict[str, FailedRunRecord] = {}
    for key, record in raw.items():
        if isinstance(record, dict):
            decoded[str(key)] = FailedRunRecord.from_payload(dict(record))
    return decoded


def _state_from_row(
    task_id: str,
    stage: str | PipelineState,
    pipeline_mode: str,
    payload: dict[str, Any],
    limits: Limits,
) -> TaskState:
    """
    Rebuild a ``TaskState`` from one ``pipeline_task_state`` row.

    Inverse of ``_state_payload``, called by ``SqlitePersistence.load``
    so the runner can resume from where the previous launch left off
    instead of starting every task in the READY state.
    """
    last_report_data = payload.get("last_report") or {}
    last_rejections_data = payload.get("last_rejection_by_stage") or {}
    failed_run_history_data = payload.get("failed_run_history") or {}
    hook_fingerprint_data = payload.get("last_hook_reject_fingerprint") or None
    active_recovery_trigger = payload.get("active_recovery_trigger")
    recovery_history = payload.get("recovery_history")
    merge_context = payload.get("merge_context")
    commit_result = payload.get("commit_result")
    rejection_loop = payload.get("rejection_loop")
    if payload.get("entry_stage") is None:
        entry_stage_value = None
    else:
        entry_stage_value = canonical_pipeline_state(payload["entry_stage"])
    if isinstance(active_recovery_trigger, dict):
        active_recovery_trigger_value = RecoveryTrigger.from_payload(dict(active_recovery_trigger))
    else:
        active_recovery_trigger_value = None
    if isinstance(merge_context, dict):
        merge_context_value = MergeContext.from_payload(dict(merge_context))
    else:
        merge_context_value = None
    if isinstance(commit_result, dict):
        commit_result_value = CommitResult.from_payload(dict(commit_result))
    else:
        commit_result_value = None
    if isinstance(rejection_loop, dict):
        rejection_loop_value = RejectionLoop.from_payload(dict(rejection_loop))
    else:
        rejection_loop_value = None
    if hook_fingerprint_data is not None:
        last_hook_reject_fingerprint_value = HookRejectFingerprint.from_payload(hook_fingerprint_data)
    else:
        last_hook_reject_fingerprint_value = None
    if payload.get("failed_reason"):
        failed_reason_value = FailedReason(payload["failed_reason"])
    else:
        failed_reason_value = None
    return TaskState(
        task_id=task_id,
        stage=canonical_pipeline_state(stage),
        pipeline_mode=PipelineMode(pipeline_mode),
        entry_stage=entry_stage_value,
        stage_retry=_decode_stage_retry_map(payload.get("stage_retry")),
        active_recovery_trigger=active_recovery_trigger_value,
        recovery_history=_decode_recovery_history(recovery_history),
        recovery_budget_history_start=int(payload.get("recovery_budget_history_start") or 0),
        pre_exec_recovery_attempt=int(payload.get("pre_exec_recovery_attempt") or 0),
        agent_elapsed_seconds=float(payload.get("agent_elapsed_seconds") or 0.0),
        merge_context=merge_context_value,
        commit_result=commit_result_value,
        last_report=LastReport.from_payload(last_report_data),
        last_rejection_by_stage=_decode_last_rejection_by_stage(last_rejections_data),
        failed_run_history=_decode_failed_run_history(failed_run_history_data),
        rejection_loop=rejection_loop_value,
        consecutive_same_hook_rejects=int(payload.get("consecutive_same_hook_rejects") or 0),
        last_hook_reject_fingerprint=last_hook_reject_fingerprint_value,
        hook_reject_recovery_invoked=bool(payload.get("hook_reject_recovery_invoked", False)),
        failed_reason=failed_reason_value,
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
        """
        Bind the persistence to a workspace plus a ``Limits`` config.

        ``Limits`` is re-injected on every ``load`` so the runner-level
        retry/budget configuration travels with the persistence object
        rather than getting baked into the persisted row; constructed
        once per runner launch so every save/load uses the same config.
        """
        self.workspace = workspace
        self.limits = limits or Limits()

    def save(self, state: TaskState) -> None:
        """
        Upsert the lifecycle cursor and emit a ``pipeline_task_state_saved`` audit event.

        Called by the runner after every state-machine step that
        mutates ``TaskState``; the audit event is what lets the events
        feed reconstruct the full sequence of state changes even when
        only the latest row survives in the cursor table.
        """
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
                    state.stage.value,
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
                        "stage": state.stage.value,
                        "pipeline_mode": state.pipeline_mode.value,
                        "payload": payload,
                        "updated_at": updated_at,
                    }
                },
            )
            connection.commit()

    def load(self, task_id: str) -> TaskState:
        """
        Return the last persisted ``TaskState`` for the task.

        Raises ``TaskNotFound`` for tasks that never reached the runner
        so the caller can decide whether to initialise a fresh state or
        treat the missing row as an error. Used by the runner on launch
        and by status/debug/diagnostics readers.
        """
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
                        reset_state.stage.value,
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
