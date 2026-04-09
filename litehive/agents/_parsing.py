"""Stage report parsing from subagent results."""

from pathlib import Path

from litehive.models import StageReport, TaskRecord, cap_feedback

from litehive.agents._models import SubagentResult


def stage_report_from_subagent(
    task: TaskRecord,
    step: str,
    result: SubagentResult,
    *,
    root: Path | None = None,
) -> StageReport:
    # Step 1: Check if agent submitted a verdict via `litehive report` CLI.
    if root is not None:
        from litehive.tasks.reports import load_task_thread

        thread = load_task_thread(root, task)
        step_comments = [c for c in thread if c.step == step and c.verdict != "comment"]
        if step_comments:
            latest = step_comments[-1]
            return StageReport(
                task_id=task.id,
                step=step,  # type: ignore[arg-type]
                verdict=latest.verdict,  # type: ignore[arg-type]
                summary=latest.message.splitlines()[0]
                if latest.message
                else f"{step} {latest.verdict}",
                feedback=latest.message,
                submitted_via_cli=True,
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

    # No CLI verdict submitted — treat agent non-completion as reject.
    return StageReport(
        task_id=task.id,
        step=step,  # type: ignore[arg-type]
        verdict="reject",
        summary=f"{step} rejected: agent did not submit verdict via litehive report CLI",
        feedback=cap_feedback(result.transcript),
        warnings=["Agent did not submit verdict via litehive report CLI."],
    )
