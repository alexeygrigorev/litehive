"""
Dataclasses describing dirty-worktree findings for the pool gate.

The pool gate runs before any new task can claim the workspace and
needs a single, machine-friendly summary of "is anything dirty, and
who owns it?". Walking the worktree tree happens in
``litehive.worktree.inspection``; the records here are what gets
persisted to the pool-state SQLite row and rendered by status output.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from litehive.domain.common import PipelineStatus, StringEnum, TaskStatus


class PoolStopReason(StringEnum):
    """
    Machine-readable reasons a pool run can stop.

    The runner persists these values and the pool summary renders them
    for operators. Keeping the label mapping on the enum prevents CLI
    code from maintaining a parallel string dictionary.
    """

    SINGLE_TASK_COMPLETE = "single_task_complete"
    QUEUE_EXHAUSTED = "queue_exhausted"
    TASK_REQUEUED = "task_requeued"
    TASK_INTERRUPTED = "task_interrupted"
    CONTINUE_OR_ROLLBACK_REQUIRED = "continue_or_rollback_required"
    BLOCKED_TASKS_REMAINING = "blocked_tasks_remaining"
    STOP_CONDITION_REACHED = "stop_condition_reached"
    MAX_TASKS_REACHED = "max_tasks_reached"
    FAILURE_DETECTED = "failure_detected"
    CONSECUTIVE_TASK_FAILURES = "consecutive_task_failures"
    DIRTY_GIT_STATE = "dirty_git_state"
    DIVERGED_FROM_ORIGIN = "diverged_from_origin"
    ATTENTION_REQUIRED = "attention_required"
    HUMAN_CHECKPOINT_BEFORE_ACCEPTANCE = "human_checkpoint_before_acceptance"
    HUMAN_CHECKPOINT_BEFORE_COMMIT = "human_checkpoint_before_commit"
    HUMAN_CHECKPOINT_REACHED = "human_checkpoint_reached"

    @classmethod
    def from_value(cls, value: str) -> "PoolStopReason | None":
        """
        Return the typed reason for known persisted values.
        """
        for stop_reason in cls:
            if stop_reason.value == value:
                return stop_reason
        return None

    @property
    def operator_label(self) -> str:
        """
        Human-readable label for pool-summary output.
        """
        match self:
            case PoolStopReason.SINGLE_TASK_COMPLETE:
                return "single task complete"
            case PoolStopReason.QUEUE_EXHAUSTED:
                return "queue exhausted"
            case PoolStopReason.TASK_REQUEUED:
                return "task requeued for another pass"
            case PoolStopReason.TASK_INTERRUPTED:
                return "task interrupted and awaiting resume"
            case PoolStopReason.CONTINUE_OR_ROLLBACK_REQUIRED:
                return "continue or rollback required"
            case PoolStopReason.BLOCKED_TASKS_REMAINING:
                return "blocked tasks remaining"
            case PoolStopReason.STOP_CONDITION_REACHED:
                return "custom stop condition reached"
            case PoolStopReason.MAX_TASKS_REACHED:
                return "max tasks reached"
            case PoolStopReason.FAILURE_DETECTED:
                return "failure detected"
            case PoolStopReason.CONSECUTIVE_TASK_FAILURES:
                return "consecutive task failures"
            case PoolStopReason.DIRTY_GIT_STATE:
                return "dirty git state"
            case PoolStopReason.DIVERGED_FROM_ORIGIN:
                return "local main diverged from origin/main"
            case PoolStopReason.ATTENTION_REQUIRED:
                return "attention required"
            case PoolStopReason.HUMAN_CHECKPOINT_BEFORE_ACCEPTANCE:
                return "human checkpoint before acceptance"
            case PoolStopReason.HUMAN_CHECKPOINT_BEFORE_COMMIT:
                return "human checkpoint before commit"
            case PoolStopReason.HUMAN_CHECKPOINT_REACHED:
                return "human checkpoint reached"

    @property
    def progress_report(self) -> "PoolProgressReport | None":
        """
        Operator-facing no-progress/action summary for stop reasons.
        """
        match self:
            case PoolStopReason.BLOCKED_TASKS_REMAINING:
                return PoolProgressReport(
                    progress_status="no_useful_progress",
                    summary="Pool stopped with no useful progress because no runnable task remained.",
                )
            case PoolStopReason.TASK_REQUEUED:
                return PoolProgressReport(
                    progress_status="no_useful_progress",
                    summary="Pool stopped with no useful progress because the active task was requeued for another pass.",
                )
            case PoolStopReason.TASK_INTERRUPTED:
                return PoolProgressReport(
                    progress_status="no_useful_progress",
                    summary="Pool stopped with no useful progress because the active task was interrupted and must be resumed.",
                )
            case PoolStopReason.CONTINUE_OR_ROLLBACK_REQUIRED:
                return PoolProgressReport(
                    progress_status="operator_action_required",
                    summary=(
                        "Pool stopped after a checkpoint commit. Continue with a new run or roll back the checkpoint "
                        "before unrelated queued work proceeds."
                    ),
                )
            case PoolStopReason.ATTENTION_REQUIRED:
                return PoolProgressReport(
                    progress_status="operator_action_required",
                    summary="Pool stopped because operator intervention is required before more work starts.",
                )
            case PoolStopReason.CONSECUTIVE_TASK_FAILURES:
                return PoolProgressReport(
                    progress_status="operator_action_required",
                    summary="Pool stopped after three consecutive task failures. Inspect the latest failed tasks before restarting.",
                )
            case _:
                return None


@dataclass(slots=True)
class PoolProgressReport:
    """
    Progress/action summary attached to non-progress pool stops.
    """

    progress_status: str
    summary: str


@dataclass(slots=True)
class PoolTaskReportEntry:
    """
    One task row in the pool summary.
    """

    task_id: str
    title: str
    final_task_status: TaskStatus | str
    pipeline_status: PipelineStatus | str
    stage_outcomes: list[str] = field(default_factory=list)
    reason_code: str | None = None
    reason: str | None = None
    follow_up_task_id: str | None = None
    close_reason: str | None = None
    flag_reason: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "PoolTaskReportEntry":
        """
        Convert a legacy report dictionary at the file/CLI boundary.
        """
        stage_outcomes_value = data.get("stage_outcomes", [])
        if isinstance(stage_outcomes_value, Sequence) and not isinstance(stage_outcomes_value, str):
            stage_outcomes = [str(item) for item in stage_outcomes_value]
        else:
            stage_outcomes = []
        return cls(
            task_id=str(data.get("task_id", "")),
            title=str(data.get("title", "")),
            final_task_status=str(data.get("final_task_status", "")),
            pipeline_status=str(data.get("pipeline_status", "")),
            stage_outcomes=stage_outcomes,
            reason_code=_optional_report_string(data.get("reason_code")),
            reason=_optional_report_string(data.get("reason")),
            follow_up_task_id=_optional_report_string(data.get("follow_up_task_id")),
            close_reason=_optional_report_string(data.get("close_reason")),
            flag_reason=_optional_report_string(data.get("flag_reason")),
        )


@dataclass(slots=True)
class PoolSummaryReport:
    """
    Structured summary of one pool run.
    """

    created_at: str
    stop_reason: str
    tasks_run: int
    completed: list[PoolTaskReportEntry] = field(default_factory=list)
    flagged: list[PoolTaskReportEntry] = field(default_factory=list)
    resumable: list[PoolTaskReportEntry] = field(default_factory=list)
    closed: list[PoolTaskReportEntry] = field(default_factory=list)
    skipped: list[PoolTaskReportEntry] = field(default_factory=list)
    remaining: list[PoolTaskReportEntry] = field(default_factory=list)
    progress_status: str | None = None
    summary: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "PoolSummaryReport":
        """
        Convert a legacy pool report dictionary to the typed report.
        """
        return cls(
            created_at=str(data.get("created_at", "")),
            stop_reason=str(data.get("stop_reason", "")),
            tasks_run=_int_report_value(data.get("tasks_run")),
            completed=_report_entries_from_value(data.get("completed")),
            flagged=_report_entries_from_value(data.get("flagged")),
            resumable=_report_entries_from_value(data.get("resumable")),
            closed=_report_entries_from_value(data.get("closed")),
            skipped=_report_entries_from_value(data.get("skipped")),
            remaining=_report_entries_from_value(data.get("remaining")),
            progress_status=_optional_report_string(data.get("progress_status")),
            summary=_optional_report_string(data.get("summary")),
        ).with_derived_progress_report()

    @property
    def stop_condition(self) -> str:
        """
        Operator-facing label for the stop reason.
        """
        known_reason = PoolStopReason.from_value(self.stop_reason)
        if known_reason is not None:
            return known_reason.operator_label
        return self.stop_reason.replace("_", " ")

    @property
    def completed_count(self) -> int:
        return len(self.completed)

    @property
    def flagged_count(self) -> int:
        return len(self.flagged)

    @property
    def resumable_count(self) -> int:
        return len(self.resumable)

    @property
    def closed_count(self) -> int:
        return len(self.closed)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def remaining_count(self) -> int:
        return len(self.remaining)

    def with_derived_progress_report(self) -> "PoolSummaryReport":
        """
        Backfill progress details from the stop reason when absent.
        """
        if self.progress_status is not None or self.summary is not None:
            return self
        known_reason = PoolStopReason.from_value(self.stop_reason)
        if known_reason is None:
            return self
        progress_report = known_reason.progress_report
        if progress_report is None:
            return self
        self.progress_status = progress_report.progress_status
        self.summary = progress_report.summary
        return self


def _optional_report_string(value: object) -> str | None:
    """
    Normalize optional report fields loaded from legacy mappings.
    """
    if value is None:
        return None
    return str(value)


def _int_report_value(value: object) -> int:
    """
    Normalize integer report fields loaded from legacy mappings.
    """
    if isinstance(value, int):
        return value
    if value is None:
        return 0
    return int(str(value))


def _report_entries_from_value(value: object) -> list[PoolTaskReportEntry]:
    """
    Convert legacy task entry lists at the file/CLI boundary.
    """
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    entries: list[PoolTaskReportEntry] = []
    for item in value:
        if isinstance(item, PoolTaskReportEntry):
            entries.append(item)
            continue
        # Boundary conversion for reports built before the typed shape.
        if isinstance(item, Mapping):
            entries.append(PoolTaskReportEntry.from_mapping(item))
    return entries


class DirtyWorktreeLocationKind(StringEnum):
    """
    Where a dirty-worktree finding was detected.

    The worktree inspector writes this and status output renders it.
    Keeping the vocabulary typed prevents the pool gate from silently
    drifting if a location label is renamed.
    """

    MAIN_CHECKOUT = "main-checkout"
    TASK_WORKTREE = "task-worktree"


class DirtyWorktreeOwnership(StringEnum):
    """
    Ownership classification for dirty worktree paths.

    ``DirtyWorktreeGateReport.blocks_pool`` uses this domain decision
    to distinguish harmless task-owned dirt from workspace-wide dirt
    that must halt the pool.
    """

    MAIN_CHECKOUT = "main-checkout"
    TASK_OWNED = "task-owned"
    AMBIGUOUS_OWNERSHIP = "ambiguous-ownership"
    MISSING_RECORDED_WORKTREE = "missing-recorded-worktree"
    TASK_OWNED_WORKTREE = "task-owned-worktree"

    @property
    def blocks_pool(self) -> bool:
        """
        Whether this ownership class is severe enough to halt the pool.
        """
        return self in {
            DirtyWorktreeOwnership.MAIN_CHECKOUT,
            DirtyWorktreeOwnership.AMBIGUOUS_OWNERSHIP,
            DirtyWorktreeOwnership.MISSING_RECORDED_WORKTREE,
        }


@dataclass(slots=True)
class DirtyWorktreeFinding:
    """
    One dirty-state finding pinned to a workspace location.

    Records *where* the dirt is (main checkout vs. a task worktree),
    *who* it belongs to (a specific task, ambiguous, or unowned), and
    the file list. The pool gate aggregates a list of these into
    ``DirtyWorktreeGateReport`` and decides whether to proceed; status
    output uses them to tell the operator which task to clean up.
    """

    location_kind: DirtyWorktreeLocationKind | str  # Where changes were found
    ownership: DirtyWorktreeOwnership | str  # Who owns the dirty paths
    dirty_paths: list[str] = field(default_factory=list)  # Specific files with changes
    task_id: str | None = None  # Associated task if changes are task-owned
    worktree_path: str | None = None  # Path to the worktree with changes

    def __post_init__(self) -> None:
        """
        Canonicalize boundary strings into the typed domain vocabulary.

        Tests and replayed status payloads may still construct findings
        from persisted string values. Convert once here so every
        consumer, especially ``DirtyWorktreeGateReport.blocks_pool``,
        reads enum members.
        """
        self.location_kind = DirtyWorktreeLocationKind(self.location_kind)
        self.ownership = DirtyWorktreeOwnership(self.ownership)


@dataclass(slots=True)
class DirtyWorktreeGateReport:
    """
    Aggregate of every dirty-worktree finding for the pool gate.

    The gate writes one of these per scan; the runner refuses to claim
    a new task if ``blocks_pool`` is true, and status output reads the
    list to render the cleanup hints. Empty findings list = clean
    workspace = pool may proceed.
    """

    findings: list[DirtyWorktreeFinding] = field(default_factory=list)  # All uncommitted change locations found

    @property
    def is_clean(self) -> bool:
        """
        True when the workspace has no dirty-worktree findings.

        Cheap "may we proceed?" check used by status and the pool gate
        before doing any per-finding routing. ``blocks_pool`` is the
        stricter check that also tolerates task-owned dirt.
        """
        return not self.findings

    @property
    def blocks_pool(self) -> bool:
        """
        True when at least one finding is severe enough to halt the pool.

        Severe = dirt on the main checkout, ambiguous task ownership,
        or a registry-tracked worktree that doesn't actually exist on
        disk. Task-owned dirt is recorded for visibility but doesn't
        block on its own — the owning task can resume its own changes.
        """
        return any(DirtyWorktreeOwnership(finding.ownership).blocks_pool for finding in self.findings)


__all__ = [
    "DirtyWorktreeFinding",
    "DirtyWorktreeGateReport",
    "DirtyWorktreeLocationKind",
    "DirtyWorktreeOwnership",
    "PoolProgressReport",
    "PoolStopReason",
    "PoolSummaryReport",
    "PoolTaskReportEntry",
]
