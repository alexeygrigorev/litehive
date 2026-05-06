"""Sealed event hierarchy for the pipeline state machine.

Every node in the state machine returns an ``Event`` from its ``run()``
method; the transition table then matches on the event's concrete type
to decide where to route. This file is the single place that declares
what events exist — adding a new event means updating this file *and*
adding the matching rule(s) in ``transitions.py``.

Each docstring answers **who fires this event and when**. Read it before
adding a new rule that matches an event, or before inventing a new event.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from litehive.domain.common import PipelineState
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.reports import SEMANTIC_REJECT_CLASSIFICATION


@dataclass(frozen=True)
class Event:
    """Base class for all transition-triggering events.

    Frozen dataclasses are used throughout so events are cheap to pass
    around, hash, and log. Direct instances of ``Event`` are never fired —
    only the concrete subclasses below.
    """

    @property
    def trigger_event_kind(self) -> TriggerEventKind:
        """
        Recovery-trigger label this event carries when a failure is
        being lifted into a ``RecoveryTrigger``.

        Subclasses override the property (or a class-level constant)
        when they have a more specific kind. The base returns
        ``UNKNOWN`` so non-failure events that accidentally reach the
        recovery layer surface clearly instead of being mis-categorised.
        """
        return TriggerEventKind.UNKNOWN

    @property
    def failure_message(self) -> str:
        """
        Human-readable reason this event terminated or escalated work.

        Read by both the terminal ``Fail`` effect (to populate
        ``failed_message``) and by ``recovery_trigger_from_event`` (to
        populate ``RecoveryTrigger.message``). Subclasses override with
        the field that carries their detail; the base returns ``""`` so
        non-failure events accidentally reaching either site stay quiet
        rather than producing a bogus message.
        """
        return ""

    @property
    def failure_source(self) -> str | None:
        """
        Source label written onto ``RecoveryTrigger.source`` for failure events.

        Only ``Reject`` carries a non-null source (the agent / hook /
        guard / system tag). All other failure events leave it ``None``
        because their cause is the runner itself rather than a verdict-
        emitting actor.
        """
        return None

    @property
    def failure_diagnostics(self) -> dict[str, Any]:
        """
        Per-event diagnostics dict written onto ``RecoveryTrigger.diagnostics``.

        Subclasses override to expose their type-specific context (the
        crash exception type, the stage that exhausted retries, the
        reject's metadata). The base returns a fresh empty dict so the
        caller can mutate without aliasing.
        """
        return {}

    @property
    def terminal_recovery_verdict(self) -> str | None:
        """
        Short verdict label persisted on ``RecoveryOutcome`` when this event
        terminates a task in ``RECOVERING``.

        Read by the terminal ``Fail`` effect so timeline views can tell
        "the agent reported failure" from "we hit the budget" from
        "recovery itself crashed" without re-deriving the cause. ``None``
        means "no event-specific label fits" so the caller falls back to
        the failed reason's value.
        """
        return None


@dataclass(frozen=True)
class Pass(Event):
    """The node succeeded at its job; advance on the happy path.

    Fired by:
      - agent nodes (grooming / implementing / testing / accepting) when
        the agent submits a ``pass`` verdict via ``litehive agent report``.
      - the ``commit`` system node when ``git merge`` lands cleanly.
      - ``merge_resolving`` (``MergeAgent``) when it resolves all the
        conflict files and commits the resolution.

    Never fired by hook nodes — they use ``HookOk`` instead so the rules
    can distinguish an agent pass from a hook phase completing.

    ``metadata`` carries the verdict's ``files_changed`` / ``tests_added``
    details from the submitted ``litehive agent report`` activity entry. The Runner
    reads it after each transition and updates ``state.last_report`` so
    downstream guards (``zero_change_shortcut``, etc.) see real numbers
    instead of defaults.
    """

    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookOk(Event):
    """The current hook phase finished and execution should continue.

    Fired only by ``HookNode`` when every configured hook passes.
    Empty hook lists also produce ``HookOk`` immediately.
    """

    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CleanState(Event):
    """The ``ready`` probe found no pre-execution trouble; enter the pipeline.

    Fired only by ``ReadyNode``. Routes ``ready → before_grooming`` in
    full mode or ``ready → before_implementing`` in single mode. The
    90%+ case for any healthy task start.
    """


@dataclass(frozen=True)
class NeedsPreExecRecovery(Event):
    """The ``ready`` probe detected broken pre-execution state.

    Fired only by ``ReadyNode`` when e.g. the worktree lock is stale,
    the task row is half-written, or a previous runner crashed mid-step.
    Routes to ``recovering_pre_exec`` which takes one shot at cleanup.
    Never fired for "dirty but meaningful" state (mid-implementing
    worktrees are expected on resume, not a failure).
    """


@dataclass(frozen=True)
class Reject(Event):
    """Some code path decided this stage's work isn't acceptable.

    Fired by any node that can produce a rejection:
      - agent nodes → ``source="agent"`` (reviewer finds a bug, QA's
        tests fail, planner can't produce a plan, etc.).
      - hook nodes → ``source="hook"`` when a runner hook exits non-zero.
      - guards → ``source="guard"`` (e.g. a future ``no_hallucinated_files``
        guard catches the SWE claiming a file it didn't touch).
      - system nodes → ``source="system"`` (currently unused — the
        commit node emits ``MergeConflictDetected`` instead so the rule
        table can route conflicts specifically).
      - ``MergeAgent`` → ``source="agent"`` when it cannot resolve the
        conflict.

    Routing depends on where it fires: retry-epoch rejects usually go
    back to implementing with a counter bump; testing may still hand off
    to accepting for a QA override when hooks are green; exhausted or
    non-retry reject paths fail directly instead of invoking recovery.
    """

    source: Literal["agent", "hook", "guard", "system"]
    reason: str
    classification: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def trigger_event_kind(self) -> TriggerEventKind:
        """
        ``SEMANTIC_REJECT`` when the reviewer/QA flagged the work as
        wrong on the merits, plain ``REJECT`` otherwise.

        The recovery agent groups repeats by this kind, so a hook
        reject must not pass for a semantic one — that distinction is
        what keeps "test setup is wonky" loops from drowning out a real
        "the SWE keeps producing the same bad code" loop.
        """
        if self.classification == SEMANTIC_REJECT_CLASSIFICATION:
            return TriggerEventKind.SEMANTIC_REJECT
        return TriggerEventKind.REJECT

    @property
    def failure_message(self) -> str:
        """The reviewer/hook/guard's free-text reject reason."""
        return self.reason

    @property
    def failure_source(self) -> str | None:
        """The actor that produced the reject (``agent``/``hook``/``guard``/``system``)."""
        return self.source

    @property
    def failure_diagnostics(self) -> dict[str, Any]:
        """A copy of the reject metadata so callers can mutate without aliasing."""
        return dict(self.metadata or {})


@dataclass(frozen=True)
class MergeConflictDetected(Event):
    """Automatic git merge hit conflicts — hand off to the merge agent.

    Fired only by ``CommitNode`` (and subclasses like ``GitCommitNode``)
    when ``git merge`` leaves files in an unresolved state. The
    conflicting file list rides along so the ``stash_conflict_files``
    effect can copy it to ``state.merge_context`` for the
    ``MergeAgent`` prompt to read.

    Routes ``commit → merge_resolving``. The worktree is deliberately
    left in the unresolved state across the hand-off so the agent has
    conflict markers to edit.
    """

    conflict_files: tuple[str, ...]


@dataclass(frozen=True)
class Blocked(Event):
    """System-detected infrastructure blockage; don't retry, route to recovery.

    Reserved for non-agent code paths that detect a blocked execution
    condition such as missing dependencies, quota exhaustion, or another
    infrastructure stop. Agent-authored verdicts no longer use
    ``blocked``; agents submit ``reject`` instead and explain what
    prevented completion.
    """

    reason: str

    @property
    def trigger_event_kind(self) -> TriggerEventKind:
        return TriggerEventKind.BLOCKED

    @property
    def failure_message(self) -> str:
        """The blockage description (missing dependency, quota, etc.)."""
        return self.reason


@dataclass(frozen=True)
class Crash(Event):
    """Unrecoverable error inside a node — escalate to the state machine.

    Fired when a node's ``run()`` method needs to report a problem that
    neither same-engine retries nor engine switches can fix. Sources:
      - ``AgentNode`` after the inner loop has exhausted engine options
        (``Crash(AllEnginesExhausted)``) or hit an ``UnrecoverableError``.
      - ``SystemNode`` subclasses on programmer-visible git errors
        (``Crash(GitError)``).
      - The runner itself if a node's ``run()`` raises an unhandled
        exception (wrapped into a Crash before the transition step).

    Wildcard-routes to ``recovering`` from any stage phase. The recovery
    agent then has its one shot at fixing whatever broke.
    """

    exc_type: str
    message: str

    @property
    def trigger_event_kind(self) -> TriggerEventKind:
        return TriggerEventKind.CRASH

    @property
    def failure_message(self) -> str:
        """The crash exception's stringified message."""
        return self.message

    @property
    def failure_diagnostics(self) -> dict[str, Any]:
        """Carry the exception class name so the recovery prompt can branch on it."""
        return {"exc_type": self.exc_type}

    @property
    def terminal_recovery_verdict(self) -> str | None:
        """A crash inside ``RECOVERING`` is the agent itself dying, not the work failing."""
        return "crash"


@dataclass(frozen=True)
class Timeout(Event):
    """A node's ``run()`` exceeded its grace period.

    Fired by the runner (not by a node itself) when a node exceeds its
    configured per-node grace period or the workspace-wide default.
    Routes to ``recovering`` from any stage phase. Currently not emitted
    anywhere in M1 — reserved for when the runner gains true timeout
    enforcement around ``node.run()`` calls.
    """

    @property
    def trigger_event_kind(self) -> TriggerEventKind:
        return TriggerEventKind.TIMEOUT

    @property
    def terminal_recovery_verdict(self) -> str | None:
        """Distinguish "recovery agent timed out" from a generic recovery failure."""
        return "timeout"


@dataclass(frozen=True)
class StageRetryLimitHit(Event):
    """A stage's retry counter reached its configured limit.

    Emitted by the runner (or by a future effect) when a stage has been
    retried more than ``Limits.stage_retry_limit`` times. Routes that
    stage to ``failed``. Currently not fired directly — the
    ``IncStageRetry`` / ``stage_retries_exhausted`` guard combo
    accomplishes the same routing by picking between two rules. Kept in
    the vocabulary so the runner can emit it explicitly if we ever
    consolidate the two-rule pattern.
    """

    stage: PipelineState

    @property
    def trigger_event_kind(self) -> TriggerEventKind:
        return TriggerEventKind.STAGE_RETRY_LIMIT

    @property
    def failure_message(self) -> str:
        """Render which stage exhausted its retry budget for failure summaries."""
        return f"Stage retry limit exhausted for {self.stage}"

    @property
    def failure_diagnostics(self) -> dict[str, Any]:
        """Carry the exhausted stage so the recovery prompt sees which counter blew."""
        return {"stage": self.stage}


@dataclass(frozen=True)
class OverallRetryLimitHit(Event):
    """Whole-task retry budget exhausted across all stages.

    Reserved for a future runner-level budget check: if a task has
    cycled through retries so many times it's clear no amount of further
    work will land it, fail directly regardless of which stage we're in.
    Not currently emitted; the rule exists so we can turn it on without
    a rule-table change.
    """

    @property
    def trigger_event_kind(self) -> TriggerEventKind:
        return TriggerEventKind.RETRY_LIMIT

    @property
    def failure_message(self) -> str:
        """Static label for the whole-task retry exhaustion case."""
        return "Overall retry limit exhausted"


@dataclass(frozen=True)
class TaskTimeBudgetExceeded(Event):
    """A task exceeded its cumulative agent wall-clock budget before commit.

    Fired by ``StateMachineRunner`` after agent-backed node execution pushes
    the task over the workspace ``task_time_budget_seconds`` limit while the
    task is still in a pre-commit pipeline phase. Routes directly to
    ``failed`` so the task layer can flag it for manual review and preserve
    its worktree.
    """

    elapsed_seconds: float
    budget_seconds: float

    @property
    def failure_message(self) -> str:
        """Render the elapsed-vs-budget numbers for the failure summary."""
        return (
            f"Task time budget exceeded before commit: "
            f"{self.elapsed_seconds:.1f}s elapsed, budget {self.budget_seconds:.1f}s"
        )


@dataclass(frozen=True)
class RecoverySucceeded(Event):
    """The recovery agent returned a successful verdict.

    Fired only by ``RecoveryAgent.verdict_to_event`` in response to a
    ``resume`` / ``advance`` / ``done`` outcome. The ``resume`` field
    tells the rule table where to route:
      - ``PipelineState.DONE`` → terminal
      - a stage name (e.g. ``"implementing"``) → that stage's pre-hook
      - a bare phase name → that phase directly
    """

    resume: PipelineState
    disposition_hint: Literal["resume", "advance", "done"] = "resume"


@dataclass(frozen=True)
class RecoveryFailed(Event):
    """The recovery agent gave up without a fix.

    Fired only by ``RecoveryAgent.verdict_to_event`` when the recovery
    verdict is anything other than ``resume`` / ``advance`` / ``done`` /
    ``budget_hit``. Routes ``recovering → failed`` with
    ``failed_reason=recovery_exhausted``.
    """

    reason: str

    @property
    def failure_message(self) -> str:
        """The recovery agent's free-text giving-up reason."""
        return self.reason

    @property
    def terminal_recovery_verdict(self) -> str | None:
        """Mark the outcome as the recovery agent itself reporting failure."""
        return "failed"


@dataclass(frozen=True)
class RecoveryBudgetHit(Event):
    """Recovery was requested for a stage that already used its one shot.

    Fired by ``RecoveryAgent.verdict_to_event`` when the agent returns
    outcome ``budget_hit``. Routes ``recovering → failed`` with
    ``failed_reason=recovery_budget_hit``. Since the pipeline enforces "one
    recovery per stage" by construction, this is currently a belt-and-
    suspenders signal — the rule table already prevents a second entry.
    """

    @property
    def terminal_recovery_verdict(self) -> str | None:
        """Distinguish "we already gave up" from generic recovery failures."""
        return "budget_hit"


@dataclass(frozen=True)
class PreExecRecoverySucceeded(Event):
    """Pre-exec recovery cleared whatever was wrong; resume the pipeline.

    Fired only by ``PreExecRecoveryNode``. ``resume_stage`` names where
    the task should land after recovery — typically ``"grooming"`` for
    full mode or ``"implementing"`` for single mode. The rule table
    routes to ``before_<resume_stage>``.
    """

    resume_stage: PipelineState


@dataclass(frozen=True)
class PreExecRecoveryFailed(Event):
    """Pre-exec recovery couldn't salvage the task.

    Fired by ``PreExecRecoveryNode`` when the repair attempt didn't fix
    the underlying issue (e.g. the worktree is irrecoverably corrupt).
    Routes ``recovering_pre_exec → failed`` with
    ``failed_reason=pre_exec_recovery_failed``.
    """

    reason: str


@dataclass(frozen=True)
class PreExecRecoveryBudgetHit(Event):
    """Pre-exec recovery was attempted a second time.

    Fired if the pre-exec budget (one attempt per task lifetime) has
    already been used. Same failure reason as ``PreExecRecoveryFailed``
    — a second attempt is always a hard failure, per the design doc.
    """
