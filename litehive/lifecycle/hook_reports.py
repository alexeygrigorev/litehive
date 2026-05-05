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
from litehive.workspace import Workspace

from .nodes.hook import HookSpec


def _normalize_hook_spec_data(hook) -> dict:
    """
    Coerce a string-or-dict hook entry into the dict shape the spec
    builder expects.

    Workspace config accepts either ``"my-cmd"`` or
    ``{"command": "my-cmd", ...}`` for hook entries, so callers don't
    have to wrap shorthand entries by hand; this helper hides that
    flexibility from ``_build_hook_spec``.
    """
    if isinstance(hook, str):
        return {"command": hook}
    return hook


def _build_hook_spec(spec_data: dict) -> HookSpec:
    """
    Construct a ``HookSpec`` from a normalized dict.

    Honours the ``None``-pass-through rule for ``description`` and
    ``instructions_on_failure`` — explicit ``None`` in config means
    "no value", not "use the default", so the helper preserves the
    distinction instead of coercing to an empty string.
    """
    if spec_data.get("description") is None:
        description = None
    else:
        description = str(spec_data["description"])
    if spec_data.get("instructions_on_failure") is None:
        instructions_on_failure = None
    else:
        instructions_on_failure = str(spec_data["instructions_on_failure"])
    return HookSpec(
        command=str(spec_data["command"]),
        timeout_seconds=float(spec_data.get("timeout_seconds", 60)),
        description=description,
        instructions_on_failure=instructions_on_failure,
    )


def hook_specs_from_config(config) -> dict[str, list[HookSpec]]:
    """
    Translate ``LitehiveConfig.runner_hooks`` into ``HookSpec`` lists.

    Used by ``orchestration.run_task`` to feed the registry; phases
    with no configured hooks are omitted from the result so the
    registry registers an empty hook list (which immediately returns
    ``HookOk``) only for the phases that actually need one.
    """
    out: dict[str, list[HookSpec]] = {}
    for phase, hooks in (getattr(config, "runner_hooks", None) or {}).items():
        specs = [_build_hook_spec(_normalize_hook_spec_data(hook)) for hook in hooks or []]
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
    """Persist a "passed with warnings" hook outcome as a stage report, activity entry, and journal line.

    Called by ``record_hook_event`` when a runner hook returns
    ``HookOk(warnings=...)``: the stage hasn't failed, but operators need
    to see the warnings everywhere reports surface (status, activity feed,
    journal) so we fan out the same content to all three sinks.
    """
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
    workspace = Workspace.from_path(root)
    report_path = record_stage_report(workspace, task, report)
    message = f"{summary}\n\n{feedback}\n\nreport: {report_path.relative_to(root)}"
    append_task_activity(
        workspace,
        task,
        TaskActivityEntry(
            role="hook",
            stage=report_stage,
            verdict="comment",
            message=message,
        ),
    )
    append_journal(
        workspace,
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
    """Persist a hook rejection as a stage report, activity entry, and journal line.

    Twin of ``_record_hook_warnings`` for the failure path: emits the
    rejection across all observability sinks with ``failure_classification=
    "hook_reject"`` plus the hook fingerprint so the recovery agent can
    detect repeated hits of the same hook.
    """
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
    workspace = Workspace.from_path(root)
    report_path = record_stage_report(workspace, task, report)
    message = f"{summary}\n\n{feedback}\n\nreport: {report_path.relative_to(root)}"
    append_task_activity(
        workspace,
        task,
        TaskActivityEntry(
            role="hook",
            stage=report_stage,
            verdict="reject",
            message=message,
        ),
    )
    append_journal(
        workspace,
        task,
        (f"Runner hook at `{phase}` rejected the stage.\nreport: `{report_path.relative_to(root)}`"),
    )
