"""Stage report parsing from subagent results."""

from pathlib import Path

from litehive.engines import get_engine
from litehive.external_cli import parse_stage_report_text
from litehive.models import StageReport, TaskRecord

from litehive.subagents._execution import SubagentResult


def stage_report_from_subagent(
    task: TaskRecord,
    step: str,
    result: SubagentResult,
    *,
    root: Path | None = None,
) -> StageReport:
    # Check if agent submitted a verdict via `litehive report`
    if root is not None:
        from litehive.tasks import load_task_thread

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
                files_changed=latest.files_changed,
            )

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
    if result.execution is not None:
        engine = get_engine(result.ref.engine)
        return engine.parse_stage_report(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            execution=result.execution,
            subagent_status=result.ref.status,
        )
    return parse_stage_report_text(
        task_id=task.id,
        step=step,  # type: ignore[arg-type]
        transcript=result.transcript,
        subagent_status=result.ref.status,
    )
