from litehive.domain.common import PipelineStatus, TaskStatus, utcnow
from litehive.state.records import list_tasks
from litehive.tasks.report_storage import load_stage_reports_for_task_id


def task_stage_outcomes(root, task_id, slug):
    del slug
    return [f"{report.pipeline_state}={report.verdict}" for report in load_stage_reports_for_task_id(root, task_id)]


def _pool_task_report_entry(
    root,
    task_id,
    title,
    status,
    pipeline_status,
    slug=None,
    reason_code=None,
    reason=None,
    follow_up_task_id=None,
    close_reason=None,
    flag_reason=None,
):
    stage_outcomes = task_stage_outcomes(root, task_id, slug) if slug is not None else []
    return {
        "task_id": task_id,
        "title": title,
        "final_task_status": status,
        "pipeline_status": pipeline_status,
        "stage_outcomes": stage_outcomes,
        "reason_code": reason_code,
        "reason": reason,
        "follow_up_task_id": follow_up_task_id,
        "close_reason": close_reason,
        "flag_reason": flag_reason,
    }


def _pending_pool_tasks(root):
    pending = []
    for task in list_tasks(root):
        if task.status in {TaskStatus.QUEUED, TaskStatus.IN_PROGRESS} and task.pipeline_status != PipelineStatus.DONE:
            pending.append(
                _pool_task_report_entry(
                    root,
                    task_id=task.id,
                    title=task.title,
                    status=task.status,
                    pipeline_status=task.pipeline_status,
                    slug=task.slug,
                )
            )
    return pending


def _resumable_pool_tasks(root):
    resumable = []
    for task in list_tasks(root):
        if task.status not in {TaskStatus.INTERRUPTED, TaskStatus.PARKED} or task.pipeline_status == PipelineStatus.DONE:
            continue
        resumable.append(
            _pool_task_report_entry(
                root,
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


def _closed_pool_tasks(root):
    closed = []
    for task in list_tasks(root):
        if task.status != TaskStatus.CLOSED:
            continue
        closed.append(
            _pool_task_report_entry(
                root,
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
    label,
    entry,
):
    stage_outcomes = [str(item) for item in entry.get("stage_outcomes", [])]
    stage_outcomes_label = ", ".join(stage_outcomes) if stage_outcomes else "-"
    line = (
        f"{label}: {entry['task_id']} {entry['title']} status={entry['final_task_status']} "
        f"pipeline_status={entry['pipeline_status']} stage_outcomes={stage_outcomes_label}"
    )
    reason_code = entry.get("reason_code")
    if reason_code:
        line += f" reason_code={reason_code}"
    close_reason = entry.get("close_reason")
    if close_reason:
        line += f" close_reason={close_reason}"
    flag_reason = entry.get("flag_reason")
    if flag_reason:
        line += f" flag_reason={flag_reason}"
    reason = entry.get("reason")
    if reason:
        line += f" reason={reason}"
    follow_up_task_id = entry.get("follow_up_task_id")
    if follow_up_task_id:
        line += f" follow_up_task={follow_up_task_id}"
    return line


def pool_stop_condition_label(stop_reason):
    labels = {
        "single_task_complete": "single task complete",
        "queue_exhausted": "queue exhausted",
        "task_requeued": "task requeued for another pass",
        "task_interrupted": "task interrupted and awaiting resume",
        "continue_or_rollback_required": "continue or rollback required",
        "blocked_tasks_remaining": "blocked tasks remaining",
        "stop_condition_reached": "custom stop condition reached",
        "max_tasks_reached": "max tasks reached",
        "failure_detected": "failure detected",
        "consecutive_task_failures": "consecutive task failures",
        "dirty_git_state": "dirty git state",
        "diverged_from_origin": "local main diverged from origin/main",
        "attention_required": "attention required",
        "human_checkpoint_before_acceptance": "human checkpoint before acceptance",
        "human_checkpoint_before_commit": "human checkpoint before commit",
        "human_checkpoint_reached": "human checkpoint reached",
    }
    return labels.get(stop_reason, stop_reason.replace("_", " "))


def _pool_no_useful_progress_report(stop_reason):
    reports = {
        "blocked_tasks_remaining": (
            "no_useful_progress",
            "Pool stopped with no useful progress because no runnable task remained.",
        ),
        "task_requeued": (
            "no_useful_progress",
            "Pool stopped with no useful progress because the active task was requeued for another pass.",
        ),
        "task_interrupted": (
            "no_useful_progress",
            "Pool stopped with no useful progress because the active task was interrupted and must be resumed.",
        ),
        "continue_or_rollback_required": (
            "operator_action_required",
            "Pool stopped after a checkpoint commit. Continue with a new run or roll back the checkpoint before unrelated queued work proceeds.",
        ),
        "attention_required": (
            "operator_action_required",
            "Pool stopped because operator intervention is required before more work starts.",
        ),
        "consecutive_task_failures": (
            "operator_action_required",
            "Pool stopped after three consecutive task failures. Inspect the latest failed tasks before restarting.",
        ),
    }
    return reports.get(stop_reason, (None, None))


def _print_pool_summary_report(
    report,
):
    report = _ensure_pool_summary_report_fields(report)
    for line in _pool_summary_report_lines(report=report):
        print(line)


def _pool_summary_report_data(
    root,
    completed,
    flagged,
    stop_reason,
    tasks_run=None,
):
    remaining = _pending_pool_tasks(root)
    resumable = _resumable_pool_tasks(root)
    closed = _closed_pool_tasks(root)
    progress_status, summary = _pool_no_useful_progress_report(stop_reason)
    return {
        "created_at": utcnow(),
        "summary": summary,
        "progress_status": progress_status,
        "stop_condition": pool_stop_condition_label(stop_reason),
        "stop_reason": stop_reason,
        "tasks_run": tasks_run if tasks_run is not None else len(completed) + len(flagged),
        "completed_count": len(completed),
        "completed": completed,
        "flagged_count": len(flagged),
        "flagged": flagged,
        "resumable_count": len(resumable),
        "resumable": resumable,
        "closed_count": len(closed),
        "closed": closed,
        "skipped_count": len(remaining),
        "skipped": remaining,
        "remaining_count": len(remaining),
        "remaining": remaining,
    }


def _pool_summary_report_lines(
    report,
):
    report = _ensure_pool_summary_report_fields(report)
    completed = [entry for entry in report["completed"] if isinstance(entry, dict)]
    flagged = [entry for entry in report["flagged"] if isinstance(entry, dict)]
    resumable = [entry for entry in report["resumable"] if isinstance(entry, dict)]
    closed = [entry for entry in report["closed"] if isinstance(entry, dict)]
    skipped = [entry for entry in report["skipped"] if isinstance(entry, dict)]
    remaining = [entry for entry in report["remaining"] if isinstance(entry, dict)]
    lines = [f"completed_tasks: {report['completed_count']}"]
    for entry in completed:
        lines.append(
            _format_pool_task_report_line(
                label="completed",
                entry=entry,
            )
        )
    lines.append(f"flagged_tasks: {report['flagged_count']}")
    for entry in flagged:
        lines.append(
            _format_pool_task_report_line(
                label="flagged",
                entry=entry,
            )
        )
    lines.append(f"resumable_tasks: {report['resumable_count']}")
    for entry in resumable:
        lines.append(
            _format_pool_task_report_line(
                label="resumable",
                entry=entry,
            )
        )
    lines.append(f"closed_tasks: {report['closed_count']}")
    for entry in closed:
        lines.append(
            _format_pool_task_report_line(
                label="closed",
                entry=entry,
            )
        )
    lines.append(f"skipped_tasks: {report['skipped_count']}")
    for entry in skipped:
        lines.append(
            _format_pool_task_report_line(
                label="skipped",
                entry=entry,
            )
        )
    lines.append(f"remaining_tasks: {report['remaining_count']}")
    for entry in remaining:
        lines.append(
            _format_pool_task_report_line(
                label="remaining",
                entry=entry,
            )
        )
    lines.append(f"tasks_run: {report['tasks_run']}")
    if report.get("progress_status") is not None:
        lines.append(f"progress_status: {report['progress_status']}")
    if report.get("summary") is not None:
        lines.append(f"summary: {report['summary']}")
    lines.append(f"stop_condition: {report['stop_condition']}")
    lines.append(f"stop_reason: {report['stop_reason']}")
    return lines


def _ensure_pool_summary_report_fields(report):
    progress_status = report.get("progress_status")
    summary = report.get("summary")
    if progress_status is not None or summary is not None:
        return report
    stop_reason = str(report.get("stop_reason", ""))
    derived_progress_status, derived_summary = _pool_no_useful_progress_report(stop_reason)
    if derived_progress_status is None and derived_summary is None:
        return report
    enriched = dict(report)
    enriched["progress_status"] = derived_progress_status
    enriched["summary"] = derived_summary
    return enriched


def _write_pool_summary_report(
    root,
    report,
):
    report = _ensure_pool_summary_report_fields(report)
    report_path = root / ".litehive" / "pool-summary.txt"
    report_lines = _pool_summary_report_lines(report=report)
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
