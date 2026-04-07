"""Stage report parsing from subagent results."""

from pathlib import Path

from litehive.engines.base import _extract_stage_result_submission
from litehive.models import StageReport, StageResultSubmission, TaskRecord, cap_feedback

from litehive.subagents._execution import SubagentResult


def _extract_stage_metadata(result: SubagentResult) -> dict:
    """Extract non-verdict metadata from STAGE_RESULT JSON in transcript."""
    # Try rendered transcript first, then raw transcript, then raw stdout.
    candidates: list[str] = []
    if result.execution is not None:
        from litehive.engines import get_engine

        engine = get_engine(result.ref.engine)
        rendered = engine.render_transcript(result.execution)
        if rendered:
            candidates.append(rendered)
    if result.transcript:
        candidates.append(result.transcript)
    if result.execution is not None and result.execution.stdout:
        candidates.append(result.execution.stdout)
    if not candidates:
        return {}

    submission = None
    for text in candidates:
        submission = _extract_stage_result_submission(text)
        if isinstance(submission, StageResultSubmission):
            break
    if not isinstance(submission, StageResultSubmission):
        return {}

    metadata: dict = {}
    if submission.files_changed:
        metadata["files_changed"] = submission.files_changed
    if submission.follow_up_tasks:
        metadata["follow_up_tasks"] = submission.follow_up_tasks
    if submission.warnings:
        metadata["warnings"] = submission.warnings
    task_update_dict = (
        submission.task_update.model_dump(exclude_unset=True) if submission.task_update else {}
    )
    if submission.acceptance_criteria and "acceptance_criteria" not in task_update_dict:
        task_update_dict["acceptance_criteria"] = submission.acceptance_criteria
    if task_update_dict:
        metadata["task_update"] = task_update_dict
    if submission.tests:
        metadata["tests"] = {"added": submission.tests.added, "passing": submission.tests.passing}
    return metadata


def stage_report_from_subagent(
    task: TaskRecord,
    step: str,
    result: SubagentResult,
    *,
    root: Path | None = None,
) -> StageReport:
    # Step 1: Check if agent submitted a verdict via `litehive report` CLI.
    if root is not None:
        from litehive.tasks import load_task_thread

        thread = load_task_thread(root, task)
        step_comments = [c for c in thread if c.step == step and c.verdict != "comment"]
        if step_comments:
            latest = step_comments[-1]
            # Verdict comes from CLI; merge non-verdict metadata from STAGE_RESULT
            # JSON in transcript if available.
            metadata = _extract_stage_metadata(result)
            files_changed = latest.files_changed or metadata.get("files_changed", [])
            return StageReport(
                task_id=task.id,
                step=step,  # type: ignore[arg-type]
                verdict=latest.verdict,  # type: ignore[arg-type]
                summary=latest.message.splitlines()[0]
                if latest.message
                else f"{step} {latest.verdict}",
                feedback=latest.message,
                files_changed=files_changed,
                follow_up_tasks=metadata.get("follow_up_tasks", []),
                task_update=metadata.get("task_update", {}),
                tests=metadata.get("tests", {}),
                warnings=metadata.get("warnings", []),
            )

    # Step 2: Resource limit events produce a blocked verdict.
    if (
        result.failure is not None
        and result.failure.kind == "resource_limit"
        and result.failure.resource_limit_event is not None
    ):
        event = result.failure.resource_limit_event
        return StageReport(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            verdict="blocked",
            summary=f"{step} blocked: {event.reason}",
            feedback=result.transcript,
            warnings=[event.reason],
            resource_limit_event=event,
        )

    # No CLI verdict submitted — verdict is fail.
    return StageReport(
        task_id=task.id,
        step=step,  # type: ignore[arg-type]
        verdict="fail",
        summary=f"{step} failed: agent did not submit verdict via litehive report CLI",
        feedback=cap_feedback(result.transcript),
        warnings=["Agent did not submit verdict via litehive report CLI."],
    )
