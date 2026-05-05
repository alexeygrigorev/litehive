"""Translate runner-hook outcomes into stage reports, journal entries, and activity rows.

Hook events (warnings, rejects) emitted by the state machine flow
through these helpers so observability surfaces (`litehive status`,
report files, journal) see the same shape as engine-emitted reports.
"""

from pathlib import Path

from litehive.domain.common import (
    PipelineState,
    canonical_pipeline_state,
    cap_feedback,
    task_stage_for_pipeline_state,
)
from litehive.domain.reports import (
    ReportPipelineState,
    StageReport,
    TaskActivityEntry,
    canonical_report_pipeline_state,
)
from litehive.domain.task import TaskRecord
from litehive.tasks.activity import append_task_activity
from litehive.tasks.journal import append_journal
from litehive.tasks.report_storage import record_stage_report

from .nodes.hook import HookSpec


def hook_specs_from_config(config) -> dict[str, list[HookSpec]]:
    """Translate ``LitehiveConfig.runner_hooks`` into ``HookSpec`` lists."""
    out: dict[str, list[HookSpec]] = {}
    for phase, hooks in (getattr(config, "runner_hooks", None) or {}).items():
        specs = [
            HookSpec(
                command=str(spec_data["command"]),
                timeout_seconds=float(spec_data.get("timeout_seconds", 60)),
                description=None if spec_data.get("description") is None else str(spec_data["description"]),
                instructions_on_failure=(
                    None
                    if spec_data.get("instructions_on_failure") is None
                    else str(spec_data["instructions_on_failure"])
                ),
            )
            for hook in hooks or []
            for spec_data in [{"command": hook} if isinstance(hook, str) else hook]
        ]
        if specs:
            out[phase] = specs
    return out


def _report_stage_for_phase(phase: str | PipelineState) -> ReportPipelineState:
    """Project a pipeline phase to the typed :class:`ReportPipelineState`.

    Hook-emitted reports use whichever stage the runner was passing
    through when the hook fired. We collapse internal node states down
    to their owning task stage, except merge-resolving and recovering,
    which are first-class report stages.
    """
    state = canonical_pipeline_state(phase)
    if state == PipelineState.MERGE_RESOLVING:
        return PipelineState.MERGE_RESOLVING.value
    if state == PipelineState.RECOVERING:
        return PipelineState.RECOVERING.value
    task_stage = task_stage_for_pipeline_state(state)
    if task_stage is None:
        # No task-stage projection exists; fall back to the report
        # converter, which raises if the value is also not a valid
        # report stage.
        return canonical_report_pipeline_state(str(state))
    return task_stage


def _record_hook_warnings(
    root: Path,
    task: TaskRecord,
    phase: str,
    warnings: list[str],
) -> None:
    report_stage = _report_stage_for_phase(phase)
    summary = f"Runner hooks at `{phase}` completed with warnings."
    feedback = "\n\n".join(warnings)
    report = StageReport(
        task_id=task.id,
        pipeline_state=report_stage,
        verdict="pass",
        source="hook",
        summary=summary,
        feedback=cap_feedback(feedback),
        warnings=warnings,
        failure_diagnostics={
            "phase": phase,
            "source": "hook",
        },
    )
    report_path = record_stage_report(root, task, report)
    message = f"{summary}\n\n{feedback}\n\nreport: {report_path.relative_to(root)}"
    append_task_activity(
        root,
        task,
        TaskActivityEntry(
            role="hook",
            stage=str(report_stage),
            verdict="comment",
            message=message,
        ),
    )
    append_journal(
        root,
        task,
        (f"Runner hooks at `{phase}` reported warnings.\nreport: `{report_path.relative_to(root)}`"),
    )


def _record_hook_reject(
    root: Path,
    task: TaskRecord,
    phase: str,
    reason: str,
    warnings: list[str],
    hook: dict[str, str] | None,
    consecutive_same_hook_rejects: int | None,
) -> None:
    report_stage = _report_stage_for_phase(phase)
    summary = f"Runner hook at `{phase}` rejected the stage."
    feedback_parts = [reason, *warnings]
    feedback = "\n\n".join(part for part in feedback_parts if part)
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = {
        "phase": phase,
        "source": "hook",
        "consecutive_same_hook_rejects": consecutive_same_hook_rejects,
    }
    if hook is not None:
        failure_diagnostics.update(
            {
                "point": hook.get("point"),
                "command": hook.get("command"),
                "description": hook.get("description"),
                "fingerprint": hook.get("fingerprint"),
            }
        )
    report = StageReport(
        task_id=task.id,
        pipeline_state=report_stage,
        verdict="reject",
        source="hook",
        summary=summary,
        feedback=cap_feedback(feedback),
        warnings=warnings,
        failure_classification="hook_reject",
        failure_diagnostics=failure_diagnostics,
    )
    report_path = record_stage_report(root, task, report)
    message = f"{summary}\n\n{feedback}\n\nreport: {report_path.relative_to(root)}"
    append_task_activity(
        root,
        task,
        TaskActivityEntry(
            role="hook",
            stage=str(report_stage),
            verdict="reject",
            message=message,
        ),
    )
    append_journal(
        root,
        task,
        (f"Runner hook at `{phase}` rejected the stage.\nreport: `{report_path.relative_to(root)}`"),
    )
