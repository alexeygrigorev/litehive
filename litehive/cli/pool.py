from collections.abc import Mapping

from litehive.domain.common import PipelineStatus, TaskStatus, utcnow
from litehive.domain.pool import PoolSummaryReport, PoolTaskReportEntry
from litehive.tasks.report_storage import load_stage_reports_for_task_id
from litehive.workspace import Workspace


def task_stage_outcomes_for_workspace(workspace: Workspace, task_id: str) -> list[str]:
    """
    Flatten a task's stored stage reports using an injected workspace.
    """
    reports = load_stage_reports_for_task_id(workspace, task_id)
    outcomes: list[str] = []
    for report in reports:
        outcomes.append(f"{report.pipeline_state}={report.verdict}")
    return outcomes


def _pool_task_report_entry_for_workspace(
    workspace: Workspace,
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
    """
    Shared pool report entry builder from an injected workspace.
    """
    if slug is not None:
        stage_outcomes = task_stage_outcomes_for_workspace(workspace, task_id)
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


def _pending_pool_tasks_for_workspace(workspace: Workspace) -> list[PoolTaskReportEntry]:
    """
    Collect pending pool tasks from an injected workspace.
    """
    pending = []
    for task in workspace.list_tasks():
        if task.is_pool_pending:
            pending.append(
                _pool_task_report_entry_for_workspace(
                    workspace,
                    task_id=task.id,
                    title=task.title,
                    status=task.status,
                    pipeline_status=task.pipeline_status,
                    slug=task.slug,
                )
            )
    return pending


def _resumable_pool_tasks_for_workspace(workspace: Workspace) -> list[PoolTaskReportEntry]:
    """
    Collect resumable pool tasks from an injected workspace.
    """
    resumable = []
    for task in workspace.list_tasks():
        if not task.is_resumable:
            continue
        resumable.append(
            _pool_task_report_entry_for_workspace(
                workspace,
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


def _closed_pool_tasks_for_workspace(workspace: Workspace) -> list[PoolTaskReportEntry]:
    """
    Collect closed pool tasks from an injected workspace.
    """
    closed = []
    for task in workspace.list_tasks():
        if not task.is_closed:
            continue
        closed.append(
            _pool_task_report_entry_for_workspace(
                workspace,
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


def _print_pool_summary_report(
    report: PoolSummaryReport | Mapping[str, object],
) -> None:
    """
    Print the pool summary to stdout instead of writing it to a file.

    Used as a debug/operator override path; no production callers
    today (candidate for removal alongside the data builder below
    once the dead-helper sweep retires both).
    """
    typed_report = _ensure_pool_summary_report_fields(report)
    for line in _pool_summary_report_lines(report=typed_report):
        print(line)


def _pool_summary_report_data_for_workspace(
    workspace: Workspace,
    completed: list[PoolTaskReportEntry],
    flagged: list[PoolTaskReportEntry],
    stop_reason: str,
    tasks_run: int | None = None,
) -> PoolSummaryReport:
    """
    Build the structured pool-summary payload from an injected workspace.
    """
    remaining = _pending_pool_tasks_for_workspace(workspace)
    resumable = _resumable_pool_tasks_for_workspace(workspace)
    closed = _closed_pool_tasks_for_workspace(workspace)
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
        resumable=resumable,
        closed=closed,
        skipped=remaining,
        remaining=remaining,
    )
    return report.with_derived_progress_report()


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


def _write_pool_summary_report(
    workspace: Workspace,
    report: PoolSummaryReport | Mapping[str, object],
) -> None:
    """
    Persist the operator-facing pool summary to a fixed file.

    Writes ``.litehive/pool-summary.txt`` so daemon runs leave
    behind a stable, scriptable artifact; operators should not
    have to recover it from stdout logs that may have been
    truncated. The path is hard-coded because it is part of the
    operator-facing contract.
    """
    report = _ensure_pool_summary_report_fields(report)
    report_path = workspace.control_dir() / "pool-summary.txt"
    report_lines = _pool_summary_report_lines(report=report)
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
