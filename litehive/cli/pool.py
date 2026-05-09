from collections.abc import Callable, Mapping
from typing import Protocol

from litehive.domain.common import PipelineState, PipelineStatus, TaskStatus, utcnow
from litehive.domain.pool import PoolSummaryReport, PoolTaskReportEntry
from litehive.state.persist import CONSECUTIVE_TASK_FAILURE_STOP_REASON, WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.tasks.report_storage import TaskReportStore
from litehive.workspace import Workspace


class PoolRunIteration(Protocol):
    """
    Minimum run-once result shape needed by the pool drain service.
    """

    exit_code: int
    ran_task: bool
    final_stage: PipelineState | None
    pool_stop_reason: str | None


class PoolService:
    """
    Workspace-bound service for pool reporting, summaries, and drain policy.
    """

    def __init__(
        self,
        workspace: Workspace,
        tasks: WorkspaceTasks | None = None,
        run_once: Callable[[str | None, str | None], PoolRunIteration] | None = None,
        dirty_checker: Callable[[], bool] | None = None,
        emit: Callable[[str], None] = print,
    ) -> None:
        self.workspace = workspace
        self.tasks = tasks or WorkspaceTasks(workspace)
        self.run_once = run_once
        self.dirty_checker = dirty_checker
        self.emit = emit

    def stage_outcomes(self, task_id: str) -> list[str]:
        reports = TaskReportStore(self.workspace).load_stage_reports_for_task_id(task_id)
        outcomes: list[str] = []
        for report in reports:
            outcomes.append(f"{report.pipeline_state}={report.verdict}")
        return outcomes

    def task_report_entry(
        self,
        task_id: str,
        title: str,
        status: TaskStatus,
        pipeline_status: PipelineStatus,
        slug: str | None = None,
        reason_code: str | None = None,
        reason: str | None = None,
        follow_up_task_id: str | None = None,
        close_reason: str | None = None,
        flag_reason: str | None = None,
    ) -> PoolTaskReportEntry:
        if slug is not None:
            stage_outcomes = self.stage_outcomes(task_id)
        else:
            stage_outcomes = []
        return PoolTaskReportEntry(
            task_id=task_id,
            title=title,
            final_task_status=status,
            pipeline_status=pipeline_status,
            stage_outcomes=stage_outcomes,
            reason_code=reason_code,
            reason=reason,
            follow_up_task_id=follow_up_task_id,
            close_reason=close_reason,
            flag_reason=flag_reason,
        )

    def collect_pending(self) -> list[PoolTaskReportEntry]:
        pending: list[PoolTaskReportEntry] = []
        for task in self.tasks.list():
            if task.is_pool_pending:
                pending.append(
                    self.task_report_entry(
                        task_id=task.id,
                        title=task.title,
                        status=task.status,
                        pipeline_status=task.pipeline_status,
                        slug=task.slug,
                    )
                )
        return pending

    def collect_resumable(self) -> list[PoolTaskReportEntry]:
        resumable: list[PoolTaskReportEntry] = []
        for task in self.tasks.list():
            if not task.is_resumable:
                continue
            resumable.append(
                self.task_report_entry(
                    task_id=task.id,
                    title=task.title,
                    status=task.status,
                    pipeline_status=task.pipeline_status,
                    slug=task.slug,
                    reason_code=task.runtime.pipeline.last_outcome.reason_code,
                    reason=task.runtime.pipeline.last_outcome.reason,
                    follow_up_task_id=task.runtime.pipeline.last_outcome.follow_up_task_id,
                )
            )
        return resumable

    def collect_closed(self) -> list[PoolTaskReportEntry]:
        closed: list[PoolTaskReportEntry] = []
        for task in self.tasks.list():
            if not task.is_closed:
                continue
            closed.append(
                self.task_report_entry(
                    task_id=task.id,
                    title=task.title,
                    status=task.status,
                    pipeline_status=task.pipeline_status,
                    slug=task.slug,
                    reason_code=task.runtime.pipeline.last_outcome.reason_code,
                    reason=task.runtime.pipeline.last_outcome.reason,
                    follow_up_task_id=task.runtime.pipeline.last_outcome.follow_up_task_id,
                    close_reason=task.close_reason,
                )
            )
        return closed

    def summarize(
        self,
        completed: list[PoolTaskReportEntry],
        flagged: list[PoolTaskReportEntry],
        stop_reason: str,
        tasks_run: int | None = None,
    ) -> PoolSummaryReport:
        remaining = self.collect_pending()
        if tasks_run is not None:
            tasks_run_value = tasks_run
        else:
            tasks_run_value = len(completed) + len(flagged)
        report = PoolSummaryReport(
            created_at=utcnow(),
            stop_reason=stop_reason,
            tasks_run=tasks_run_value,
            completed=completed,
            flagged=flagged,
            resumable=self.collect_resumable(),
            closed=self.collect_closed(),
            skipped=remaining,
            remaining=remaining,
        )
        return report.with_derived_progress_report()

    def render_summary(self, report: PoolSummaryReport | Mapping[str, object]) -> list[str]:
        return self.render_summary_report(report)

    @staticmethod
    def render_summary_report(report: PoolSummaryReport | Mapping[str, object]) -> list[str]:
        return _pool_summary_report_lines(report)

    def write_summary(self, report: PoolSummaryReport | Mapping[str, object]) -> None:
        report = _ensure_pool_summary_report_fields(report)
        report_path = self.workspace.control_dir() / "pool-summary.txt"
        report_lines = self.render_summary(report)
        report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    def print_summary(self, report: PoolSummaryReport | Mapping[str, object]) -> None:
        self.print_summary_report(report, emit=self.emit)

    @staticmethod
    def print_summary_report(
        report: PoolSummaryReport | Mapping[str, object],
        emit: Callable[[str], None] = print,
    ) -> None:
        for line in PoolService.render_summary_report(report):
            emit(line)

    def stop_for(self, reason: str) -> None:
        WorkspaceStateRepository(self.workspace).set_pool_stop_reason(reason)

    def run(
        self,
        engine: str | None = None,
        model: str | None = None,
        stop_on_failure: bool = False,
        limit: int | None = None,
        stop_on_dirty_git: bool = False,
    ) -> int:
        if self.run_once is None:
            raise RuntimeError("PoolService.run requires a run_once callback")
        tasks_run = 0
        while True:
            if stop_on_dirty_git and self.dirty_checker is not None and self.dirty_checker():
                self.stop_for("dirty_git_state")
                self.emit("Pool stopped: dirty_git_state")
                return 0

            iteration = self.run_once(engine, model)
            if iteration.exit_code != 0:
                return iteration.exit_code
            if iteration.pool_stop_reason == CONSECUTIVE_TASK_FAILURE_STOP_REASON:
                self.emit(f"Pool stopped: {CONSECUTIVE_TASK_FAILURE_STOP_REASON}")
                return 0
            if not iteration.ran_task:
                if tasks_run == 0:
                    state = WorkspaceStateRepository(self.workspace).load()
                    if state.queue:
                        self.emit("No runnable task.")
                    else:
                        self.emit("No queued task.")
                return 0

            tasks_run += 1
            if stop_on_failure and iteration.final_stage != PipelineState.DONE:
                self.stop_for("failure_detected")
                self.emit("Pool stopped: failure_detected")
                return 0
            if limit is not None and tasks_run >= limit:
                self.stop_for("max_tasks_reached")
                self.emit("Pool stopped: max_tasks_reached")
                return 0

def _format_pool_task_report_line(
    label: str,
    entry: PoolTaskReportEntry,
) -> str:
    """
    Render one task entry as a single operator-facing line.

    Reason, close_reason, flag_reason, and follow-up task id are
    only appended when set so empty fields do not pad every line —
    a fixed-width layout would inflate the summary for the common
    case. ``label`` is the bucket name (completed, flagged, …)
    that prefixes the line so a flat ``grep label:`` walks one
    bucket cleanly.
    """
    stage_outcomes = [str(item) for item in entry.stage_outcomes]
    if stage_outcomes:
        stage_outcomes_label = ", ".join(stage_outcomes)
    else:
        stage_outcomes_label = "-"
    line = (
        f"{label}: {entry.task_id} {entry.title} status={entry.final_task_status} "
        f"pipeline_status={entry.pipeline_status} stage_outcomes={stage_outcomes_label}"
    )
    reason_code = entry.reason_code
    if reason_code:
        line += f" reason_code={reason_code}"
    close_reason = entry.close_reason
    if close_reason:
        line += f" close_reason={close_reason}"
    flag_reason = entry.flag_reason
    if flag_reason:
        line += f" flag_reason={flag_reason}"
    reason = entry.reason
    if reason:
        line += f" reason={reason}"
    follow_up_task_id = entry.follow_up_task_id
    if follow_up_task_id:
        line += f" follow_up_task={follow_up_task_id}"
    return line


def _pool_summary_report_lines(
    report: PoolSummaryReport | Mapping[str, object],
) -> list[str]:
    """
    Render the operator-facing pool-summary text.

    Bucket order (completed, flagged, resumable, closed, skipped,
    remaining) follows the operator's triage flow, not alphabet:
    the operator first wants to confirm what completed, then which
    failed, then what they need to act on. Reordering the buckets
    would change muscle memory for everyone reading these summaries.
    """
    typed_report = _ensure_pool_summary_report_fields(report)
    lines = [f"completed_tasks: {typed_report.completed_count}"]
    for entry in typed_report.completed:
        lines.append(
            _format_pool_task_report_line(
                label="completed",
                entry=entry,
            )
        )
    lines.append(f"flagged_tasks: {typed_report.flagged_count}")
    for entry in typed_report.flagged:
        lines.append(
            _format_pool_task_report_line(
                label="flagged",
                entry=entry,
            )
        )
    lines.append(f"resumable_tasks: {typed_report.resumable_count}")
    for entry in typed_report.resumable:
        lines.append(
            _format_pool_task_report_line(
                label="resumable",
                entry=entry,
            )
        )
    lines.append(f"closed_tasks: {typed_report.closed_count}")
    for entry in typed_report.closed:
        lines.append(
            _format_pool_task_report_line(
                label="closed",
                entry=entry,
            )
        )
    lines.append(f"skipped_tasks: {typed_report.skipped_count}")
    for entry in typed_report.skipped:
        lines.append(
            _format_pool_task_report_line(
                label="skipped",
                entry=entry,
            )
        )
    lines.append(f"remaining_tasks: {typed_report.remaining_count}")
    for entry in typed_report.remaining:
        lines.append(
            _format_pool_task_report_line(
                label="remaining",
                entry=entry,
            )
        )
    lines.append(f"tasks_run: {typed_report.tasks_run}")
    if typed_report.progress_status is not None:
        lines.append(f"progress_status: {typed_report.progress_status}")
    if typed_report.summary is not None:
        lines.append(f"summary: {typed_report.summary}")
    lines.append(f"stop_condition: {typed_report.stop_condition}")
    lines.append(f"stop_reason: {typed_report.stop_reason}")
    return lines


def _ensure_pool_summary_report_fields(report: PoolSummaryReport | Mapping[str, object]) -> PoolSummaryReport:
    """
    Backfill ``progress_status``/``summary`` on legacy persisted reports.

    Older persisted summaries did not carry these fields; without
    backfilling, re-rendering an older report would drop the
    operator-action prose entirely. Reports that already carry
    both fields pass through unchanged.
    """
    if isinstance(report, PoolSummaryReport):
        typed_report = report
    else:
        # Boundary conversion for summaries built before PoolSummaryReport existed.
        typed_report = PoolSummaryReport.from_mapping(report)
    return typed_report.with_derived_progress_report()
