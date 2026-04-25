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

from .types import NodeName


@dataclass(frozen=True)
class Event:
    """Base class for all transition-triggering events.

    Frozen dataclasses are used throughout so events are cheap to pass
    around, hash, and log. Direct instances of ``Event`` are never fired —
    only the concrete subclasses below.
    """


@dataclass(frozen=True)
class Pass(Event):
    """The node succeeded at its job; advance on the happy path.

    Fired by:
      - agent nodes (grooming / implementing / testing / accepting) when
        the agent submits a ``pass`` verdict via ``litehive report``.
      - the ``commit`` system node when ``git merge`` lands cleanly.
      - ``merge_resolving`` (``MergeAgent``) when it resolves all the
        conflict files and commits the resolution.

    Never fired by hook nodes — they use ``HookOk`` instead so the rules
    can distinguish an agent pass from a hook phase completing.

    ``metadata`` carries the verdict's ``files_changed`` / ``tests_added``
    details from the submitted ``litehive report`` activity entry. The Runner
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


@dataclass(frozen=True)
class Timeout(Event):
    """A node's ``run()`` exceeded its grace period.

    Fired by the runner (not by a node itself) when a node exceeds its
    configured per-node grace period or the workspace-wide default.
    Routes to ``recovering`` from any stage phase. Currently not emitted
    anywhere in M1 — reserved for when the runner gains true timeout
    enforcement around ``node.run()`` calls.
    """


@dataclass(frozen=True)
class StageRetryLimitHit(Event):
    """A stage's retry counter reached its configured limit.

    Emitted by the runner (or by a future effect) when a stage has been
    retried more than ``Limits.stage_retry_limit`` times. Routes that
    stage to ``failed``. Currently not fired directly — the
    ``inc_stage_retry`` / ``stage_retries_exhausted`` guard combo
    accomplishes the same routing by picking between two rules. Kept in
    the vocabulary so the runner can emit it explicitly if we ever
    consolidate the two-rule pattern.
    """

    stage: NodeName


@dataclass(frozen=True)
class OverallRetryLimitHit(Event):
    """Whole-task retry budget exhausted across all stages.

    Reserved for a future runner-level budget check: if a task has
    cycled through retries so many times it's clear no amount of further
    work will land it, fail directly regardless of which stage we're in.
    Not currently emitted; the rule exists so we can turn it on without
    a rule-table change.
    """


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


@dataclass(frozen=True)
class RecoverySucceeded(Event):
    """The recovery agent returned a successful verdict.

    Fired only by ``RecoveryAgent.verdict_to_event`` in response to a
    ``resume`` / ``advance`` / ``done`` outcome. The ``resume`` field
    tells the rule table where to route:
      - ``"done"`` → terminal
      - a stage name (e.g. ``"implementing"``) → that stage's pre-hook
      - a bare phase name → that phase directly
    """

    resume: NodeName | Literal["done"]
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


@dataclass(frozen=True)
class RecoveryBudgetHit(Event):
    """Recovery was requested for a stage that already used its one shot.

    Fired by ``RecoveryAgent.verdict_to_event`` when the agent returns
    outcome ``budget_hit``. Routes ``recovering → failed`` with
    ``failed_reason=recovery_budget_hit``. Since v2 enforces "one
    recovery per stage" by construction, this is currently a belt-and-
    suspenders signal — the rule table already prevents a second entry.
    """


@dataclass(frozen=True)
class PreExecRecoverySucceeded(Event):
    """Pre-exec recovery cleared whatever was wrong; resume the pipeline.

    Fired only by ``PreExecRecoveryNode``. ``resume_stage`` names where
    the task should land after recovery — typically ``"grooming"`` for
    full mode or ``"implementing"`` for single mode. The rule table
    routes to ``before_<resume_stage>``.
    """

    resume_stage: NodeName


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
