from dataclasses import dataclass, field
from typing import Any, Protocol

from .types import FailedReason, NodeName, PipelineMode


@dataclass(frozen=True)
class Limits:
    stage_retry_limit: int = 3
    same_engine_retry_limit: int = 3
    overall_retry_limit: int = 30
    grace_period_seconds: int = 120


@dataclass
class LastReport:
    files_changed: int = 0
    tests_added: int = 0


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
