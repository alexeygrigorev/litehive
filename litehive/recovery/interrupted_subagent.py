"""Helpers that snapshot a still-running subagent into an ``interrupted`` record."""

from litehive.agents.execution_trace import load_subagent_execution_trace
from litehive.agents.session_store import (
    load_subagent_report,
    save_subagent_artifacts,
)
from litehive.agents.session_continuation import subagent_continuation_state
from litehive.agents.session_snapshots import InterruptedSubagentSessionRow, SubagentSessionStorageFields
from litehive.sandbox.launcher import SandboxPolicySummary
from litehive.domain.common import SubagentStatus, utcnow
from litehive.domain.runtime import RuntimeSubagentState
from litehive.domain.task import TaskRecord
from litehive.tasks.runtime import summarize_transcript
from litehive.workspace import Workspace


def mark_interrupted_subagent(
    workspace: Workspace, task: TaskRecord, reason: str, stage: str
) -> RuntimeSubagentState | None:
    """
    Promote the task's running subagent to ``interrupted`` and persist artifacts.

    A later resume sees a frozen subagent record instead of a
    half-running one; returns ``None`` when there is nothing to
    interrupt — repair callers use that to decide whether the
    interruption originated in the runner itself or in a subagent.
    """
    active = task.runtime.execution.active_subagent
    interruption = task.runtime.execution.interruption
    if interruption is None:
        existing = None
    else:
        existing = interruption.subagent
    if active is None and (existing is None or existing.status != SubagentStatus.INTERRUPTED):
        return None
    now = utcnow()
    source = active or existing
    assert source is not None
    if active is not None:
        for ref in reversed(task.subagents):
            if ref.id == active.id and ref.status == SubagentStatus.RUNNING.value:
                ref.status = SubagentStatus.INTERRUPTED.value
                break
    snippet = source.execution_trace_snippet
    if active is not None or not snippet:
        snippet = _interrupted_subagent_snippet(workspace, task, source)
    interrupted = source.model_copy(
        update={
            "status": SubagentStatus.INTERRUPTED,
            "updated_at": now,
            "completed_at": source.completed_at or now,
            "execution_trace_snippet": snippet,
            "interruption_reason": _interrupted_subagent_reason(task, reason),
        }
    )
    task.runtime.execution.active_subagent = None
    _write_interrupted_subagent_artifacts(workspace, task, interrupted, resume_stage=stage)
    return interrupted


def _interrupted_subagent_snippet(
    workspace: Workspace, task: TaskRecord, active: RuntimeSubagentState
) -> str:
    """
    Pick the best available transcript snippet for the interrupted record.

    Prefers a saved report summary, then a freshly summarised execution
    trace, then a fixed fallback string so the journal entry is never
    empty; an empty snippet would render as a missing reason in the
    operator-facing interruption journal.
    """
    report = load_subagent_report(workspace, task.id, active.id)
    if report:
        summary = str(report.get("summary") or "").strip()
        if summary:
            return summary
    ref = next((candidate for candidate in reversed(task.subagents) if candidate.id == active.id), None)
    if ref is None:
        return active.execution_trace_snippet or "runner interrupted before subagent completion"
    trace = load_subagent_execution_trace(workspace, task, ref, active=True, runtime_state=active)
    if trace is None:
        snippet = ""
    else:
        snippet = summarize_transcript(trace.text)
    if snippet:
        return snippet
    return active.execution_trace_snippet or "runner interrupted before subagent completion"


def _interrupted_subagent_reason(task: TaskRecord, reason: str) -> str:
    """
    Preserve a prior interruption reason on re-interruption of the same subagent.

    A second repair pass otherwise would overwrite the original cause
    with the generic "stale runner" label and the operator would lose
    the more specific failure context (semantic reject, hook failure,
    etc.) that the first pass captured.
    """
    active = task.runtime.execution.active_subagent
    interruption = task.runtime.execution.interruption
    if interruption is None:
        last_interrupted = None
    else:
        last_interrupted = interruption.subagent
    if (
        last_interrupted is not None
        and last_interrupted.interruption_reason
        and (active is None or last_interrupted.id == active.id)
    ):
        return last_interrupted.interruption_reason
    return reason


def _write_interrupted_subagent_artifacts(
    workspace: Workspace,
    task: TaskRecord,
    subagent: RuntimeSubagentState,
    resume_stage: str,
) -> None:
    """
    Persist the interrupted subagent's session and report files.

    Writes them in place so disk artifacts and the in-memory task
    record agree; the resume flow reads these to decide whether to
    continue the subagent or re-run it from scratch, and a divergence
    between memory and disk would mean resume picks the wrong action.
    """
    now = utcnow()
    existing_session = workspace.load_subagent_session_record(task.id, subagent.id)
    report_payload = load_subagent_report(workspace, task.id, subagent.id)
    continuation_state = subagent_continuation_state(subagent.continuation)
    created_at = str(existing_session.created_at or subagent.started_at)
    resource_control_value = existing_session.values.get("resource_control")
    if isinstance(resource_control_value, dict):
        resource_control = SandboxPolicySummary.from_mapping(resource_control_value)
    else:
        resource_control = SandboxPolicySummary(enabled=False)
    session_row = InterruptedSubagentSessionRow(
        fields=SubagentSessionStorageFields(
            id=subagent.id,
            role=subagent.role,
            engine=subagent.engine,
            status=SubagentStatus(subagent.status),
            sandboxed=subagent.sandboxed,
            sandbox=subagent.sandbox_summary or "host",
            created_at=created_at,
            updated_at=now,
            resource_control=resource_control,
        ),
        pid=subagent.pid,
        exit_code=subagent.exit_code,
        interruption_reason=subagent.interruption_reason or "interrupted",
        resume_stage=resume_stage,
        continuation=continuation_state,
    )
    report_payload["status"] = subagent.status
    report_payload["summary"] = report_payload.get("summary") or subagent.execution_trace_snippet
    report_payload["interruption_reason"] = subagent.interruption_reason or None
    report_payload["resume_stage"] = resume_stage
    report_payload["continuation"] = continuation_state.payload()
    save_subagent_artifacts(
        workspace,
        task.id,
        subagent.id,
        session=session_row,
        report=report_payload,
    )
