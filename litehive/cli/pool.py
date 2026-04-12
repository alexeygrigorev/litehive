import yaml

from litehive.models import utcnow
from litehive.tasks.crud import list_tasks


def _fmt_seconds(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, rem = divmod(s, 60)
    if m < 60:
        return f"{m}m{rem:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _task_stage_outcomes(root, task_id, slug):
    reports_dir = root / ".litehive" / "tasks" / f"{task_id}-{slug}" / "reports"
    if not reports_dir.exists():
        return []

    outcomes = []
    report_paths = sorted(
        reports_dir.glob("*.yaml"),
        key=lambda path: int(path.stem.rsplit("-", 1)[1]) if "-" in path.stem else 0,
    )
    for report_path in report_paths:
        report_data = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        step = str(report_data.get("step") or "").strip()
        verdict = str(report_data.get("verdict") or "").strip()
        if step and verdict:
            outcomes.append(f"{step}={verdict}")
    return outcomes


def _task_reports_dir(root, task_id):
    """Return the reports directory for a task, searching by task_id prefix."""
    tasks_dir = root / ".litehive" / "tasks"
    if not tasks_dir.exists():
        return None
    for folder in sorted(tasks_dir.iterdir()):
        if folder.is_dir() and folder.name.startswith(f"{task_id}-"):
            return folder / "reports"
    return None


def _collect_task_stage_stats(root, task_id):
    """Collect step/verdict/duration_seconds from all reports for a task."""
    reports_dir = _task_reports_dir(root, task_id)
    if reports_dir is None or not reports_dir.exists():
        return []
    stats = []
    report_paths = sorted(
        reports_dir.glob("*.yaml"),
        key=lambda path: int(path.stem.rsplit("-", 1)[1]) if "-" in path.stem else 0,
    )
    for report_path in report_paths:
        try:
            data = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        step = str(data.get("step") or "").strip()
        verdict = str(data.get("verdict") or "").strip()
        if not step or not verdict:
            continue
        raw_dur = data.get("duration_seconds", 0)
        duration = float(raw_dur) if isinstance(raw_dur, (int, float)) and raw_dur > 0 else 0.0
        stats.append({"step": step, "verdict": verdict, "duration_seconds": duration})
    return stats


def _compute_pool_flow_statistics(root, task_entries):
    """Compute aggregate flow statistics from all task stage reports.

    Returns a dict with stages_executed, per-stage duration metrics (avg_seconds,
    min_seconds, max_seconds), pass/fail counts, bottleneck_stage, and
    bottleneck_avg_seconds, or None when no duration data is available.
    """
    stage_durations: dict[str, list[float]] = {}
    stage_pass_counts: dict[str, int] = {}
    stage_fail_counts: dict[str, int] = {}
    total_stages = 0

    for entry in task_entries:
        task_id = entry.get("task_id")
        if not task_id:
            continue
        for stat in _collect_task_stage_stats(root, task_id):
            step = stat["step"]
            verdict = stat["verdict"]
            duration = stat["duration_seconds"]
            total_stages += 1
            if duration > 0:
                stage_durations.setdefault(step, []).append(duration)
            if verdict in ("pass", "accept"):
                stage_pass_counts[step] = stage_pass_counts.get(step, 0) + 1
            else:
                stage_fail_counts[step] = stage_fail_counts.get(step, 0) + 1

    if not stage_durations:
        return None

    stage_metrics: dict[str, dict[str, float]] = {}
    for step, durs in stage_durations.items():
        stage_metrics[step] = {
            "avg_seconds": sum(durs) / len(durs),
            "min_seconds": min(durs),
            "max_seconds": max(durs),
        }

    # Tie-break by retry (fail) count descending so the stage with more retries wins.
    bottleneck_stage = max(
        stage_metrics,
        key=lambda s: (stage_metrics[s]["avg_seconds"], stage_fail_counts.get(s, 0)),
    )
    bottleneck_avg_seconds = stage_metrics[bottleneck_stage]["avg_seconds"]

    return {
        "stages_executed": total_stages,
        "stage_metrics": stage_metrics,
        "stage_pass_counts": stage_pass_counts,
        "stage_fail_counts": stage_fail_counts,
        "bottleneck_stage": bottleneck_stage,
        "bottleneck_avg_seconds": bottleneck_avg_seconds,
    }


def _pool_task_report_entry(
    root,
    *,
    task_id,
    title,
    status,
    pipeline_status,
    slug=None,
    reason_code=None,
    reason=None,
    follow_up_task_id=None,
):
    stage_outcomes = _task_stage_outcomes(root, task_id, slug) if slug is not None else []
    return {
        "task_id": task_id,
        "title": title,
        "final_task_status": status,
        "pipeline_status": pipeline_status,
        "stage_outcomes": stage_outcomes,
        "reason_code": reason_code,
        "reason": reason,
        "follow_up_task_id": follow_up_task_id,
    }


def _pending_pool_tasks(root):
    pending = []
    for task in list_tasks(root):
        if task.status in {"queued", "in_progress"} and task.pipeline_status != "done":
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
        if task.status not in {"interrupted", "parked"} or task.pipeline_status == "done":
            continue
        resumable.append(
            _pool_task_report_entry(
                root,
                task_id=task.id,
                title=task.title,
                status=task.status,
                pipeline_status=task.pipeline_status,
                slug=task.slug,
                reason_code=task.runtime.last_outcome.reason_code,
                reason=task.runtime.last_outcome.reason,
                follow_up_task_id=task.runtime.last_outcome.follow_up_task_id,
            )
        )
    return resumable


def _closed_pool_tasks(root):
    closed = []
    for task in list_tasks(root):
        if task.status not in {"wont_do", "deferred", "duplicate"}:
            continue
        closed.append(
            _pool_task_report_entry(
                root,
                task_id=task.id,
                title=task.title,
                status=task.status,
                pipeline_status=task.pipeline_status,
                slug=task.slug,
                reason_code=task.runtime.last_outcome.reason_code,
                reason=task.runtime.last_outcome.reason,
                follow_up_task_id=task.runtime.last_outcome.follow_up_task_id,
            )
        )
    return closed


def _format_pool_task_report_line(
    *,
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
        "execution_limit_reached": "execution limit reached",
        "execution_limit_fallbacks_exhausted": "execution-limit fallbacks exhausted",
        "dirty_git_state": "dirty git state",
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
            "Pool stopped because unresolved attention items require operator action before more work starts.",
        ),
        "execution_limit_fallbacks_exhausted": (
            "no_useful_progress",
            "Pool stopped with no useful progress because engine fallbacks were exhausted.",
        ),
    }
    return reports.get(stop_reason, (None, None))


def _print_pool_summary_report(
    *,
    report,
):
    report = _ensure_pool_summary_report_fields(report)
    for line in _pool_summary_report_lines(report=report):
        print(line)


def _pool_summary_report_data(
    root,
    *,
    completed,
    flagged,
    stop_reason,
    tasks_run=None,
):
    remaining = _pending_pool_tasks(root)
    resumable = _resumable_pool_tasks(root)
    closed = _closed_pool_tasks(root)
    progress_status, summary = _pool_no_useful_progress_report(stop_reason)
    flow_statistics = _compute_pool_flow_statistics(root, list(completed) + list(flagged))
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
        "flow_statistics": flow_statistics,
    }


def _pool_summary_report_lines(
    *,
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
    flow_statistics = report.get("flow_statistics")
    if flow_statistics is not None:
        bottleneck = flow_statistics.get("bottleneck_stage")
        bottleneck_avg = flow_statistics.get("bottleneck_avg_seconds")
        bottleneck_label = (
            f"{bottleneck} (avg={_fmt_seconds(bottleneck_avg)})"
            if bottleneck and bottleneck_avg is not None
            else bottleneck or "-"
        )
        lines.append(
            f"flow_statistics: stages_executed={flow_statistics['stages_executed']} bottleneck={bottleneck_label}"
        )
        stage_metrics = flow_statistics.get("stage_metrics") or {}
        if stage_metrics:
            duration_parts = [
                f"{step}=avg:{_fmt_seconds(m['avg_seconds'])},min:{_fmt_seconds(m['min_seconds'])},max:{_fmt_seconds(m['max_seconds'])}"
                for step, m in stage_metrics.items()
            ]
            lines.append(f"stage_durations: {' '.join(duration_parts)}")
        stage_fail_counts = flow_statistics.get("stage_fail_counts") or {}
        if stage_fail_counts:
            fail_parts = [f"{step}={count}" for step, count in stage_fail_counts.items()]
            lines.append(f"stage_failures: {' '.join(fail_parts)}")
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
    *,
    root,
    report,
):
    report = _ensure_pool_summary_report_fields(report)
    report_path = root / ".litehive" / "pool-summary.txt"
    report_lines = _pool_summary_report_lines(report=report)
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    _write_durable_pool_run_report(root, report=report)


def _write_durable_pool_run_report(root, *, report):
    reports_dir = root / ".litehive" / "logs" / "pool-runs"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = str(report["created_at"]).replace(":", "-").replace("+00:00", "Z")
    report_path = reports_dir / f"{timestamp}.yaml"
    suffix = 1
    while report_path.exists():
        suffix += 1
        report_path = reports_dir / f"{timestamp}-{suffix:02d}.yaml"
    report_path.write_text(yaml.safe_dump(report, sort_keys=False), encoding="utf-8")
