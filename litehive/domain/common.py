"""
Domain vocabulary and timestamp/feedback helpers.

The enums here (``PipelineState``, ``TaskStage``, ``PipelineStatus``,
``PipelineMode``, ``TaskStatus``, ``OutcomeKind``, ``OutcomeReasonCode``,
``Verdict``, ``RunnerStatus``, ``TriggerEventKind`` partner) are the
typed alternative to passing raw strings around — code-style rule
"Domain Values" forbids string-comparing pipeline state and friends
because renames would rot silently. Convert at the boundary using
``canonical_pipeline_state``, ``task_stage_for_pipeline_state``, and
``pipeline_status_for_pipeline_state``.

``utcnow`` is the project-wide source of "now" so persisted timestamps
stay text-comparable; ``cap_feedback`` truncates long agent feedback
to fit prompt context windows without losing the pointer to the full
trace.
"""

from datetime import UTC, datetime
from enum import Enum

from heru.types import (
    EngineLimitKind,
    EngineMonitoringSource,
    LiveEventKind,
    LiveEventRole,
    SubagentStatus,
)


FEEDBACK_CAP = 2000
TRUNCATION_MARKER = "\n\n… [truncated — full execution trace in subagent artifacts]"


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


def cap_feedback(text: str, limit: int = FEEDBACK_CAP) -> str:
    """
    Truncate long subagent feedback for inclusion in a prompt.

    Replacing the tail with ``TRUNCATION_MARKER`` keeps prompts under
    engine context limits while pointing readers (and downstream agents)
    at the full execution-trace artifact. Used by every prompt that
    quotes prior agent output back to the next agent — a missing cap
    would let one chatty agent crash the next one's context budget.
    """
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


# ── litehive-native task-lifecycle vocabularies ─────────────────────────────


class OutcomeKind(StringEnum):
    """
    Terminal outcome categories for a finished task.

    Tells the operator (and downstream filters/reports) why a task is no
    longer running: completion vs. operator close vs. blocked vs.
    cancelled vs. duplicate. Set by ``TaskService`` and the CLI close
    actions; consumed by reporting, queue filtering, and the recovery
    decision of "is there anything to do for this task?".
    """

    DONE = "done"  # Task was already or successfully completed
    CLOSED = "closed"  # Explicitly closed with a close_reason
    FLAGGED = "flagged"  # Requires explicit operator attention
    BLOCKED = "blocked"  # Progress requires external input or missing dependency
    INTERRUPTED = "interrupted"  # Execution stopped, potentially resumable
    CANCELLED = "cancelled"  # Operator intentionally stopped this task
    WONT_DO = "wont_do"  # Task is no longer worth doing
    DEFERRED = "deferred"  # Task should wait for later
    DUPLICATE = "duplicate"  # Another task already covers the same work


class OutcomeReasonCode(StringEnum):
    """
    Normalized reason codes for stage outcomes and task interruptions.

    ``Verdict`` answers "was this accepted?" (pass/reject/blocked);
    ``OutcomeReasonCode`` answers "what specifically caused that
    outcome?" so two rejections with different root causes can be
    distinguished for routing and reporting. Read by ``PipelineRunner``
    when deciding whether to retry, recover, or fail; surfaced to
    operators in failure summaries and machine-filterable reports.
    """

    VERDICT_FAIL = "verdict_fail"
    VERDICT_REJECT = "verdict_reject"
    VERDICT_BLOCKED = "verdict_blocked"
    BLOCKED_ON_FOLLOW_UP = "blocked_on_follow_up"
    HALLUCINATED_COMPLETION = "hallucinated_completion"
    MISSING_ACCEPTANCE_CRITERIA = "missing_acceptance_criteria"
    RETRY_LIMIT_EXHAUSTED = "retry_limit_exhausted"
    STAGE_RETRY_LIMIT_EXHAUSTED = "stage_retry_limit_exhausted"
    EXECUTION_INTERRUPTED = "execution_interrupted"
    EXECUTION_CANCELLED = "execution_cancelled"
    STAGE_EXCEPTION = "stage_exception"
    UNSUPPORTED_VERDICT = "unsupported_verdict"
    MERGE_CONFLICT = "merge_conflict"
    DONE = "done"
    WONT_DO = "wont_do"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"


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
        return _STAGE_OWNER_ROLES[self]


_STAGE_OWNER_ROLES: dict["TaskStage", str] = {}


def _populate_stage_owners() -> None:
    """
    Fill the stage→owner-role lookup after ``TaskStage`` is fully defined.

    Wrapped as a function rather than a class-body literal so the table
    can reference enum members directly without forward-reference
    ceremony; called once at module import. The mapping powers
    ``TaskStage.owner_role`` and is the single source of truth for
    "which subagent owns which stage" across prompts and reporting.
    """
    # Populated after the enum is defined to avoid forward-reference
    # ceremony with the enum members.
    _STAGE_OWNER_ROLES.update(
        {
            TaskStage.GROOMING: "planner",
            TaskStage.IMPLEMENTING: "swe",
            TaskStage.TESTING: "qa",
            TaskStage.ACCEPTING: "reviewer",
            TaskStage.COMMIT_TO_GIT: "runner",
        }
    )


_populate_stage_owners()


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


class PipelineStatus(StringEnum):
    """
    Operator-facing projection of pipeline progress.

    Not the state machine itself: this collapses detailed
    ``PipelineState`` nodes (including before/after hooks, recovery
    sub-states, and merge resolution) into the coarse buckets shown in
    CLI status and persisted on task runtime for filtering. Renaming an
    internal pipeline state should not require updating this enum,
    which is why the projection table is explicit
    (``pipeline_status_for_pipeline_state``) rather than name-derived.
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


_TASK_STAGE_BY_PIPELINE_STATE: dict[PipelineState, TaskStage] = {
    PipelineState.BEFORE_GROOMING: TaskStage.GROOMING,
    PipelineState.GROOMING: TaskStage.GROOMING,
    PipelineState.AFTER_GROOMING: TaskStage.GROOMING,
    PipelineState.RECOVERING: TaskStage.GROOMING,
    PipelineState.BEFORE_IMPLEMENTING: TaskStage.IMPLEMENTING,
    PipelineState.IMPLEMENTING: TaskStage.IMPLEMENTING,
    PipelineState.AFTER_IMPLEMENTING: TaskStage.IMPLEMENTING,
    PipelineState.BEFORE_TESTING: TaskStage.TESTING,
    PipelineState.TESTING: TaskStage.TESTING,
    PipelineState.AFTER_TESTING: TaskStage.TESTING,
    PipelineState.BEFORE_ACCEPTING: TaskStage.ACCEPTING,
    PipelineState.ACCEPTING: TaskStage.ACCEPTING,
    PipelineState.AFTER_ACCEPTING: TaskStage.ACCEPTING,
    PipelineState.COMMIT: TaskStage.COMMIT_TO_GIT,
    PipelineState.AFTER_COMMIT: TaskStage.COMMIT_TO_GIT,
    PipelineState.MERGE_RESOLVING: TaskStage.COMMIT_TO_GIT,
}


_PIPELINE_STATUS_BY_PIPELINE_STATE: dict[PipelineState, PipelineStatus] = {
    PipelineState.READY: PipelineStatus.BACKLOG,
    PipelineState.WORKTREE_SYNC: PipelineStatus.BACKLOG,
    PipelineState.RECOVERING_PRE_EXEC: PipelineStatus.BACKLOG,
    PipelineState.BEFORE_GROOMING: PipelineStatus.GROOMING,
    PipelineState.GROOMING: PipelineStatus.GROOMING,
    PipelineState.AFTER_GROOMING: PipelineStatus.GROOMING,
    PipelineState.RECOVERING: PipelineStatus.GROOMING,
    PipelineState.BEFORE_IMPLEMENTING: PipelineStatus.IMPLEMENTING,
    PipelineState.IMPLEMENTING: PipelineStatus.IMPLEMENTING,
    PipelineState.AFTER_IMPLEMENTING: PipelineStatus.IMPLEMENTING,
    PipelineState.BEFORE_TESTING: PipelineStatus.TESTING,
    PipelineState.TESTING: PipelineStatus.TESTING,
    PipelineState.AFTER_TESTING: PipelineStatus.TESTING,
    PipelineState.BEFORE_ACCEPTING: PipelineStatus.ACCEPTING,
    PipelineState.ACCEPTING: PipelineStatus.ACCEPTING,
    PipelineState.AFTER_ACCEPTING: PipelineStatus.ACCEPTING,
    PipelineState.COMMIT: PipelineStatus.COMMIT_TO_GIT,
    PipelineState.AFTER_COMMIT: PipelineStatus.COMMIT_TO_GIT,
    PipelineState.MERGE_RESOLVING: PipelineStatus.COMMIT_TO_GIT,
    PipelineState.DONE: PipelineStatus.DONE,
    PipelineState.FAILED: PipelineStatus.FLAGGED,
}


def task_stage_for_pipeline_state(value: str | PipelineState) -> TaskStage | None:
    """
    Return the operator-facing ``TaskStage`` for an internal pipeline state.

    Used by reporting and prompt scaffolding to bucket per-state activity
    into the stage operators recognize (groom/build/test/accept/commit).
    Returns ``None`` for system-only nodes (``READY``, ``DONE``, ``FAILED``,
    ``WORKTREE_SYNC``…) that don't belong to any user-visible stage, so
    callers can distinguish "between stages" from "in stage X".
    """
    return _TASK_STAGE_BY_PIPELINE_STATE.get(canonical_pipeline_state(value))


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
    return _TASK_STAGE_BY_PIPELINE_STATE.get(state, name)


def pipeline_status_for_pipeline_state(value: str | PipelineState) -> PipelineStatus:
    """
    Project an internal ``PipelineState`` to the operator-facing ``PipelineStatus``.

    Called whenever a state-machine transition needs to update the
    runtime's coarse progress bucket — e.g. moving into ``BEFORE_TESTING``
    should still display as ``TESTING`` to the operator. Raises
    ``KeyError`` on an unmapped state so a missing entry is caught at
    the boundary instead of silently rendering as ``BACKLOG``.
    """
    return _PIPELINE_STATUS_BY_PIPELINE_STATE[canonical_pipeline_state(value)]


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


class Verdict(StringEnum):
    """
    Decision submitted for an executable pipeline state.

    Created by subagents and hook execution paths when they submit the
    result of a pipeline state, then read by ``PipelineRunner`` to
    decide whether to advance, retry, block, or enter recovery. Also
    persisted on ``ActivityEntry`` / ``TaskOutcome`` as the canonical
    submitted decision. ``StageReport`` collapses this richer set into
    its narrower ``pass/reject/blocked`` form via
    ``canonical_stage_report_verdict`` so report storage stays small
    while activity history keeps the full vocabulary.
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


RunnerExecutionStatus = RunnerStatus


__all__ = [
    "EngineLimitKind",
    "EngineMonitoringSource",
    "FEEDBACK_CAP",
    "LiveEventKind",
    "LiveEventRole",
    "OutcomeKind",
    "OutcomeReasonCode",
    "PipelineState",
    "PipelineMode",
    "PipelineStatus",
    "RunnerStatus",
    "RunnerExecutionStatus",
    "SubagentStatus",
    "TaskStage",
    "TaskStatus",
    "TRUNCATION_MARKER",
    "Verdict",
    "cap_feedback",
    "canonical_pipeline_state",
    "pipeline_stage_key",
    "pipeline_status_for_pipeline_state",
    "task_stage_for_pipeline_state",
    "utcnow",
]
