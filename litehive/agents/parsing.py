"""Stage report parsing from subagent results."""

from pathlib import Path

from litehive.domain.common import cap_feedback
from litehive.domain.reports import StageReport
from litehive.domain.task import TaskRecord

from litehive.domain.agent import SubagentResult
from litehive.tasks.activity import latest_task_activity_entry


def stage_report_from_subagent(
    task: TaskRecord,
    stage: str,
    result: SubagentResult,
    *,
    root: Path | None = None,
) -> StageReport:
    # Step 1: Check if agent submitted a verdict via `litehive report` CLI.
    if root is not None:
        latest = latest_task_activity_entry(
            root,
            task,
            stage=stage,
            verdicts={"pass", "reject", "blocked", "resume", "advance", "done", "budget_hit"},
        )
        if latest is not None:
            return StageReport(
                task_id=task.id,
                stage=stage,  # type: ignore[arg-type]
                verdict=latest.verdict,  # type: ignore[arg-type]
                summary=latest.message.splitlines()[0]
                if latest.message
                else f"{stage} {latest.verdict}",
                feedback=latest.message,
                submitted_via_cli=True,
                files_changed=list(latest.files_changed),
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
            stage=stage,  # type: ignore[arg-type]
            verdict="blocked",
            summary=f"{stage} blocked: {event.reason}",
            feedback=result.transcript,
            warnings=[event.reason],
            resource_limit_event=event,
        )

    # No CLI verdict submitted — treat agent non-completion as reject.
    return StageReport(
        task_id=task.id,
        stage=stage,  # type: ignore[arg-type]
        verdict="reject",
        summary=f"{stage} rejected: agent did not submit verdict via litehive report CLI",
        feedback=cap_feedback(result.transcript),
        warnings=["Agent did not submit verdict via litehive report CLI."],
    )
