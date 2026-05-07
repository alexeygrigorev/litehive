"""
Domain vocabulary and timestamp helpers.

The enums here (``PipelineState``, ``TaskStage``, ``PipelineStatus``,
``PipelineMode``, ``TaskStatus``, ``Verdict``, ``RunnerStatus``,
``TriggerEventKind`` partner) are the typed alternative to passing
raw strings around — code-style rule
"Domain Values" forbids string-comparing pipeline state and friends
because renames would rot silently. Convert at the boundary using
``canonical_pipeline_state``, ``task_stage_for_pipeline_state``, and
``pipeline_status_for_pipeline_state``.

``utcnow`` is the project-wide source of "now" so persisted timestamps
stay text-comparable.
"""

from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from heru.types import (
    EngineLimitKind,
    EngineMonitoringSource,
    LiveEventKind,
    LiveEventRole,
)


class StringEnum(str, Enum):
    """
    Base class for string-valued enums used across persisted models.

    Inheriting from ``str`` means an enum member compares equal to its
    underlying string, so SQLite rows and JSON payloads round-trip
    cleanly without per-field conversion. The custom ``__str__`` keeps
    f-strings and ``json.dumps`` matching the persisted spelling instead
    of leaking ``ClassName.MEMBER`` text into stored data.
    """

    def __str__(self) -> str:
        """
        Render the enum as its underlying string value.

        Without this override Python's default would produce
        ``ClassName.MEMBER``, which is not what the database, JSON
        artifacts, or operator-facing logs expect to see.
        """
        return self.value


def utcnow() -> str:
    """
    Project-wide source of "now" as a UTC ISO string.

    Microseconds are trimmed so SQLite text ordering and ``--diff`` output
    of two snapshots stay stable; every persisted timestamp in the
    workspace goes through this helper so two records written in the
    same second compare exactly equal instead of differing by jitter.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# ── litehive-native task-lifecycle vocabularies ─────────────────────────────


class PipelineMode(StringEnum):
    """
    Top-level execution mode for a task: full pipeline vs. single stage.

    ``FULL`` runs grooming through commit; ``SINGLE`` skips planning and
    starts in implementation, which is what operators want for
    follow-ups that already have a precise spec. Set at task creation or
    via operator task-edit; ``PipelineRunner`` reads this when deciding
    which pipeline states are eligible for the task.
    """

    SINGLE = "single"  # Skip early planning states, start directly in implementation
    FULL = "full"  # Run the full pipeline from grooming through commit


class PipelineState(StringEnum):
    """
    Canonical internal state-machine positions.

    These are the real nodes the pipeline runner persists, evaluates in
    transition rules, and passes into prompt templates. Kept distinct
    from ``PipelineStatus`` and ``TaskStage`` (the operator-facing
    projections) so we can rename or split internal nodes — e.g. carve
    a new ``BEFORE_*`` hook node — without churning what the operator
    sees on ``litehive status``.
    """

    READY = "ready"
    WORKTREE_SYNC = "worktree_sync"
    RECOVERING_PRE_EXEC = "recovering_pre_exec"

    BEFORE_GROOMING = "before_grooming"
    GROOMING = "grooming"
    AFTER_GROOMING = "after_grooming"

    BEFORE_IMPLEMENTING = "before_implementing"
    IMPLEMENTING = "implementing"
    AFTER_IMPLEMENTING = "after_implementing"

    BEFORE_TESTING = "before_testing"
    TESTING = "testing"
    AFTER_TESTING = "after_testing"

    BEFORE_ACCEPTING = "before_accepting"
    ACCEPTING = "accepting"
    AFTER_ACCEPTING = "after_accepting"

    COMMIT = "commit"
    AFTER_COMMIT = "after_commit"
    MERGE_RESOLVING = "merge_resolving"

    RECOVERING = "recovering"
    DONE = "done"
    FAILED = "failed"

    @property
    def human_label(self) -> str:
        """
        Operator-readable phrase for this state ("before grooming", "implementing", …).

        Used by prompt builders that surface the state to the agent in
        prose ("After <state>, these checks will run:") — keeping the
        de-snake-case mapping on the enum stops prompt code from
        hand-rolling ``state.value.replace("_", " ")`` in multiple places
        and gives us one location to special-case any state whose
        underlying spelling does not read well as plain English.
        """
        return self.value.replace("_", " ")

    @property
    def task_stage(self) -> "TaskStage | None":
        """
        Operator-facing task stage that contains this machine state.

        Hook states collapse into their owning work phase so prompts,
        reports, and recovery logic can reason in the same stage
        buckets operators see. System-only states return ``None``
        because they do not belong to a user-visible work phase.
        """
        match self:
            case PipelineState.BEFORE_GROOMING | PipelineState.GROOMING | PipelineState.AFTER_GROOMING:
                return TaskStage.GROOMING
            case PipelineState.RECOVERING:
                return TaskStage.GROOMING
            case (
                PipelineState.BEFORE_IMPLEMENTING
                | PipelineState.IMPLEMENTING
                | PipelineState.AFTER_IMPLEMENTING
            ):
                return TaskStage.IMPLEMENTING
            case PipelineState.BEFORE_TESTING | PipelineState.TESTING | PipelineState.AFTER_TESTING:
                return TaskStage.TESTING
            case PipelineState.BEFORE_ACCEPTING | PipelineState.ACCEPTING | PipelineState.AFTER_ACCEPTING:
                return TaskStage.ACCEPTING
            case PipelineState.COMMIT | PipelineState.AFTER_COMMIT | PipelineState.MERGE_RESOLVING:
                return TaskStage.COMMIT_TO_GIT
            case (
                PipelineState.READY
                | PipelineState.WORKTREE_SYNC
                | PipelineState.RECOVERING_PRE_EXEC
                | PipelineState.DONE
                | PipelineState.FAILED
            ):
                return None

    @property
    def pipeline_status(self) -> "PipelineStatus":
        """
        Operator-facing progress bucket for this machine state.

        Lifecycle runtime sync writes this projection after every
        transition. Keeping it on ``PipelineState`` makes the
        state/status relationship explicit instead of hiding it in a
        side table that every caller has to remember to use.
        """
        match self:
            case PipelineState.READY | PipelineState.WORKTREE_SYNC | PipelineState.RECOVERING_PRE_EXEC:
                return PipelineStatus.BACKLOG
            case PipelineState.BEFORE_GROOMING | PipelineState.GROOMING | PipelineState.AFTER_GROOMING:
                return PipelineStatus.GROOMING
            case PipelineState.RECOVERING:
                return PipelineStatus.GROOMING
            case (
                PipelineState.BEFORE_IMPLEMENTING
                | PipelineState.IMPLEMENTING
                | PipelineState.AFTER_IMPLEMENTING
            ):
                return PipelineStatus.IMPLEMENTING
            case PipelineState.BEFORE_TESTING | PipelineState.TESTING | PipelineState.AFTER_TESTING:
                return PipelineStatus.TESTING
            case PipelineState.BEFORE_ACCEPTING | PipelineState.ACCEPTING | PipelineState.AFTER_ACCEPTING:
                return PipelineStatus.ACCEPTING
            case PipelineState.COMMIT | PipelineState.AFTER_COMMIT | PipelineState.MERGE_RESOLVING:
                return PipelineStatus.COMMIT_TO_GIT
            case PipelineState.DONE:
                return PipelineStatus.DONE
            case PipelineState.FAILED:
                return PipelineStatus.FLAGGED

    @property
    def primary_stage(self) -> "PipelineState":
        """
        Primary executable stage that owns this pipeline phase.

        Before/after hook phases are attributed to the stage they wrap
        so lifecycle comparisons and prompt scaffolding can ask whether
        two phases belong to the same agent-run stage. Phases that are
        already primary return themselves.
        """
        match self:
            case PipelineState.BEFORE_GROOMING | PipelineState.AFTER_GROOMING:
                return PipelineState.GROOMING
            case PipelineState.BEFORE_IMPLEMENTING | PipelineState.AFTER_IMPLEMENTING:
                return PipelineState.IMPLEMENTING
            case PipelineState.BEFORE_TESTING | PipelineState.AFTER_TESTING:
                return PipelineState.TESTING
            case PipelineState.BEFORE_ACCEPTING | PipelineState.AFTER_ACCEPTING:
                return PipelineState.ACCEPTING
            case PipelineState.AFTER_COMMIT:
                return PipelineState.COMMIT
            case PipelineState.RECOVERING:
                return PipelineState.GROOMING
            case _:
                return self

    @property
    def accepts_runner_hook(self) -> bool:
        """
        Whether workspace runner hooks may attach to this phase.

        Hooks are allowed only at explicit before/after boundaries
        where the lifecycle can pause safely around an agent-owned
        stage. Recovery and merge-resolution are executable stages,
        but they are not operator hook points because they hijack
        control flow to repair an existing failure.
        """
        match self:
            case (
                PipelineState.BEFORE_GROOMING
                | PipelineState.AFTER_GROOMING
                | PipelineState.BEFORE_IMPLEMENTING
                | PipelineState.AFTER_IMPLEMENTING
                | PipelineState.BEFORE_TESTING
                | PipelineState.AFTER_TESTING
                | PipelineState.BEFORE_ACCEPTING
                | PipelineState.AFTER_ACCEPTING
                | PipelineState.AFTER_COMMIT
            ):
                return True
            case _:
                return False


class TaskStage(StringEnum):
    """
    Operator-facing work phases in the task lifecycle.

    The five stages collapse the dozens of internal ``PipelineState``
    nodes (hooks, before/after pairs, recovery sub-states) into the
    buckets an operator actually thinks in: groom, build, test, accept,
    commit. Used by status output, stage reports, and the
    role-by-stage owner mapping (planner/swe/qa/reviewer/runner) below.
    """

    GROOMING = "grooming"  # Initial planning and requirement analysis
    IMPLEMENTING = "implementing"  # Core development work
    TESTING = "testing"  # Validation and testing phase
    ACCEPTING = "accepting"  # Final review and acceptance
    COMMIT_TO_GIT = "commit_to_git"  # Git commit and merge operations

    @property
    def owner_role(self) -> str:
        """
        Subagent role that owns this stage (planner/swe/qa/reviewer/runner).

        The mapping is fixed: it expresses *who* runs this stage, not
        *what* they do. Lives on the enum so prompt builders and
        scaffolding code can look up ownership directly instead of
        carrying their own copy of the same lookup table — that
        duplication is the kind of domain prose ``code-style.md``
        forbids in prompt modules.
        """
        match self:
            case TaskStage.GROOMING:
                return "planner"
            case TaskStage.IMPLEMENTING:
                return "swe"
            case TaskStage.TESTING:
                return "qa"
            case TaskStage.ACCEPTING:
                return "reviewer"
            case TaskStage.COMMIT_TO_GIT:
                return "runner"

    @property
    def retry_counter_state(self) -> PipelineState:
        """
        Canonical pipeline state that owns this stage's retry counter.

        Before/after hook states share the same budget as their
        executable stage. Lifecycle recovery uses this state both when
        bumping retry counts and when clearing them, so the counter
        identity belongs on the stage rather than in a lifecycle-side
        lookup table.
        """
        match self:
            case TaskStage.GROOMING:
                return PipelineState.GROOMING
            case TaskStage.IMPLEMENTING:
                return PipelineState.IMPLEMENTING
            case TaskStage.TESTING:
                return PipelineState.TESTING
            case TaskStage.ACCEPTING:
                return PipelineState.ACCEPTING
            case TaskStage.COMMIT_TO_GIT:
                return PipelineState.COMMIT


def runner_hook_points() -> frozenset[str]:
    """
    Return config spellings for lifecycle phases that accept runner hooks.
    """
    return frozenset(state.value for state in PipelineState if state.accepts_runner_hook)


class TaskStatus(StringEnum):
    """
    High-level execution or terminal category for a task.

    Drives queueing (``QUEUED`` is eligible to run), filtering
    (``FLAGGED`` requires operator attention), and end-of-life routing
    (``DONE``/``CLOSED`` are terminal). Set by ``TaskService``,
    ``PipelineRunner``, and operator CLI commands.

    Note: there is no separate ``cancelled`` status because
    ``close_reason`` already explains why a task was closed; carrying
    both would force every consumer to treat them as equivalent.
    """

    QUEUED = "queued"  # Waiting in the queue
    IN_PROGRESS = "in_progress"  # Currently executing
    INTERRUPTED = "interrupted"  # Execution stopped, potentially resumable
    PARKED = "parked"  # Intentionally paused by Litehive or operator
    DONE = "done"  # Completed successfully
    CLOSED = "closed"  # Explicitly closed with a close_reason
    FLAGGED = "flagged"  # Requires explicit operator attention


class TaskExecutionStatus(StringEnum):
    """
    Per-task runner execution marker persisted on ``TaskRuntime.pipeline``.

    Distinct from ``TaskStatus`` and daemon ``RunnerStatus``: this answers
    what happened to the latest task run (idle/running/done/cancelled/etc.)
    so queue selection and recovery can tell an active run from a stale or
    terminal runtime row without string-comparing ad hoc literals.
    """

    IDLE = "idle"  # No task run is active
    RUNNING = "running"  # The task is currently executing
    PAUSED = "paused"  # The task was parked or paused
    QUEUED = "queued"  # The task should be requeued
    INTERRUPTED = "interrupted"  # The run stopped and may resume
    DONE = "done"  # The run completed successfully
    CANCELLED = "cancelled"  # The run was explicitly cancelled
    FAILED = "failed"  # The run failed terminally
    BLOCKED = "blocked"  # The run is blocked on external input
    FLAGGED = "flagged"  # The run ended by flagging the task


class RuntimeStageStatus(StringEnum):
    """
    Fine-grained status for ``TaskRuntime.pipeline.current_stage``.

    This differs from ``TaskExecutionStatus``: the task run can be queued or
    done while the current stage marker is just idle/running/interrupted.
    """

    IDLE = "idle"  # No stage is actively running
    RUNNING = "running"  # A stage is currently running
    INTERRUPTED = "interrupted"  # The stage was interrupted and may resume
    COMPLETED = "completed"  # The stage completed
    FAILED = "failed"  # The stage failed


class SubagentStatus(StringEnum):
    """
    Lifecycle status for a Litehive-managed subagent run.

    Heru accepts the same string values at the adapter boundary; inside
    Litehive, use this enum instead of hard-coded status strings so
    comparisons and assignments stay on the domain vocabulary.
    """

    CREATED = "created"  # Subagent record has been allocated
    RUNNING = "running"  # Engine process is active
    COMPLETED = "completed"  # Engine process exited successfully
    FAILED = "failed"  # Engine process failed or timed out
    BLOCKED = "blocked"  # Engine process hit an external/system block
    INTERRUPTED = "interrupted"  # Engine process was interrupted and may resume


class PipelineStatus(StringEnum):
    """
    Operator-facing projection of pipeline progress.

    Not the state machine itself: this collapses detailed
    ``PipelineState`` nodes (including before/after hooks, recovery
    sub-states, and merge resolution) into the coarse buckets shown in
    CLI status and persisted on task runtime for filtering. Renaming an
    internal pipeline state should not require updating this enum;
    ``PipelineState.pipeline_status`` owns the explicit projection.
    """

    BACKLOG = "backlog"  # Not yet started
    GROOMING = "grooming"  # In planning phase
    IMPLEMENTING = "implementing"  # In development phase
    TESTING = "testing"  # In validation phase
    ACCEPTING = "accepting"  # In review phase
    COMMIT_TO_GIT = "commit_to_git"  # In git operations phase
    DONE = "done"  # Completed successfully
    FLAGGED = "flagged"  # Requires operator attention


def canonical_pipeline_state(value: str | PipelineState) -> PipelineState:
    """
    Normalize a persisted or caller-supplied value to ``PipelineState``.

    Boundary helper used by every load-side path that pulls a state
    string out of SQLite, JSON, or a CLI argument: convert to the typed
    enum once at entry so the rest of the code can compare members
    directly without sprinkling ``str(...)`` casts. Raises ``ValueError``
    on unknown spellings, matching the "fail loud on invalid current
    config" rule rather than silently substituting a default.
    """
    if isinstance(value, PipelineState):
        return value
    return PipelineState(str(value))


def task_stage_for_pipeline_state(value: str | PipelineState) -> TaskStage | None:
    """
    Return the operator-facing ``TaskStage`` for an internal pipeline state.

    Used by reporting and prompt scaffolding to bucket per-state activity
    into the stage operators recognize (groom/build/test/accept/commit).
    Returns ``None`` for system-only nodes (``READY``, ``DONE``, ``FAILED``,
    ``WORKTREE_SYNC``…) that don't belong to any user-visible stage, so
    callers can distinguish "between stages" from "in stage X".
    """
    return canonical_pipeline_state(value).task_stage


def pipeline_stage_key(name: str | None) -> TaskStage | str | None:
    """
    Collapse any pipeline-state name to its coarse ``TaskStage`` key.

    Used by recovery, prompt serialization, and lifecycle deltas to
    bucket per-state activity into the operator-facing stage it belongs
    to. Returns the original ``name`` (or ``None``) when the input is
    not a known pipeline state, so callers can keep funneling arbitrary
    keys (e.g. raw recovery labels like ``recovering``) through a
    single helper without a separate guard branch at every call site.
    """
    if name is None:
        return None
    try:
        state = PipelineState(name)
    except ValueError:
        return name
    return state.task_stage or name


def pipeline_status_for_pipeline_state(value: str | PipelineState) -> PipelineStatus:
    """
    Project an internal ``PipelineState`` to the operator-facing ``PipelineStatus``.

    Called whenever a state-machine transition needs to update the
    runtime's coarse progress bucket — e.g. moving into ``BEFORE_TESTING``
    should still display as ``TESTING`` to the operator. Raises
    ``KeyError`` on an unmapped state so a missing entry is caught at
    the boundary instead of silently rendering as ``BACKLOG``.
    """
    return canonical_pipeline_state(value).pipeline_status


class RunnerStatus(StringEnum):
    """
    Health states for the top-level runner process.

    Surfaced by ``litehive status`` and the daemon's pre-spawn check so
    an operator can tell a fresh idle runner from a wedged one. ``LATE``
    means the heartbeat is overdue but still inside the grace window;
    ``STALE`` means we have given up on the runner and the daemon is
    free to reclaim the workspace.
    """

    IDLE = "idle"  # Runner is active but not executing a task
    RUNNING = "running"  # Runner is actively executing a task
    LATE = "late"  # Runner missed expected heartbeat timing
    STALE = "stale"  # Runner appears to be unresponsive


class TransientFailureKind(StringEnum):
    """
    Retry-eligible transient failure categories.

    Engine adapters attach these stable values to ``TransientError`` so
    retry policy can compare typed domain values instead of maintaining
    string allow-lists in config code.
    """

    EXECUTION_LIMIT = "execution_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    SERVICE = "service"


class Verdict(StringEnum):
    """
    Decision submitted for an executable pipeline state.

    Created by subagents and hook execution paths when they submit the
    result of a pipeline state, then read by `PipelineRunner` to
    decide whether to advance, retry, block, or enter recovery. Also
    persisted on `ActivityEntry` as the canonical submitted decision.
    `StageReport` collapses this richer set into its narrower
    `pass/reject/blocked` form via `canonical_stage_report_verdict` so
    report storage stays small
    while activity history keeps the full vocabulary.

    `FAIL` is the generic negative verdict kept for older hook and
    activity vocabulary; `REJECT` is the explicit agent/reviewer
    decision that a submitted stage result is not acceptable. Both
    project to a stage-report `reject`. Neither is a task outcome:
    task-level terminal state is recorded with `TaskOutcomeKind` and
    `OutcomeReasonCode`.
    """

    PASS = "pass"  # General positive outcome
    ACCEPT = "accept"  # Stage goal was achieved
    FAIL = "fail"  # General negative outcome
    REJECT = "reject"  # Result not acceptable, but can continue
    BLOCKED = "blocked"  # Progress requires external operator input
    COMMENT = "comment"  # Informational, no decision
    RESUME = "resume"  # Continue from where left off
    ADVANCE = "advance"  # Move to next stage
    DONE = "done"  # Task completed successfully
    BUDGET_HIT = "budget_hit"  # Resource limits reached

    @property
    def stage_report_verdict(self) -> Literal["pass", "reject", "blocked"] | None:
        """
        Verdict projection accepted by the compact StageReport model.

        Activity entries keep the full verdict vocabulary, but persisted
        stage reports only need pass/reject/blocked. A comment verdict
        returns `None` because it is operator-visible commentary, not
        a stage decision.
        """
        match self:
            case Verdict.PASS | Verdict.ACCEPT | Verdict.RESUME | Verdict.ADVANCE | Verdict.DONE:
                return "pass"
            case Verdict.REJECT | Verdict.FAIL:
                return "reject"
            case Verdict.BLOCKED | Verdict.BUDGET_HIT:
                return "blocked"
            case Verdict.COMMENT:
                return None


RunnerExecutionStatus = RunnerStatus


__all__ = [
    "EngineLimitKind",
    "EngineMonitoringSource",
    "LiveEventKind",
    "LiveEventRole",
    "PipelineState",
    "PipelineMode",
    "PipelineStatus",
    "RunnerStatus",
    "RunnerExecutionStatus",
    "RuntimeStageStatus",
    "SubagentStatus",
    "TaskExecutionStatus",
    "TaskStage",
    "TaskStatus",
    "TransientFailureKind",
    "Verdict",
    "canonical_pipeline_state",
    "pipeline_stage_key",
    "pipeline_status_for_pipeline_state",
    "task_stage_for_pipeline_state",
    "utcnow",
]
