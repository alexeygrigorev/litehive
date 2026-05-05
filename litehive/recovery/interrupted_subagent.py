"""Helpers that snapshot a still-running subagent into an ``interrupted`` record."""

from pathlib import Path

from litehive.agents.execution_trace import load_subagent_execution_trace
from litehive.agents.session_store import (
    load_subagent_report,
    load_subagent_session,
    save_subagent_artifacts,
)
from litehive.domain.common import utcnow
from litehive.domain.runtime import RuntimeSubagentState
from litehive.domain.task import TaskRecord
from litehive.tasks.runtime import summarize_transcript
from litehive.workspace import Workspace


def mark_interrupted_subagent(root: Path, task: TaskRecord, reason: str, stage: str) -> RuntimeSubagentState | None:
    """Promote the task's currently-running subagent to ``interrupted`` and persist the snapshot artifacts so a later resume sees a frozen subagent record instead of a half-running one. Returns ``None`` if there is nothing to interrupt — repair callers use that to decide whether the interruption originated in the runner or in a subagent."""
    active = task.runtime.execution.active_subagent
    interruption = task.runtime.execution.interruption
    if interruption is None:
        existing = None
    else:
        existing = interruption.subagent
    if active is None and (existing is None or existing.status != "interrupted"):
        return None
    now = utcnow()
    source = active or existing
    assert source is not None
    if active is not None:
        for ref in reversed(task.subagents):
            if ref.id == active.id and ref.status == "running":
                ref.status = "interrupted"
                break
    snippet = source.execution_trace_snippet
    if active is not None or not snippet:
        snippet = _interrupted_subagent_snippet(root, task, source)
    interrupted = source.model_copy(
        update={
            "status": "interrupted",
            "updated_at": now,
            "completed_at": source.completed_at or now,
            "execution_trace_snippet": snippet,
            "interruption_reason": _interrupted_subagent_reason(task, reason),
        }
    )
    task.runtime.execution.active_subagent = None
    _write_interrupted_subagent_artifacts(root, task, interrupted, resume_stage=stage)
    return interrupted


def _interrupted_subagent_snippet(root: Path, task: TaskRecord, active: RuntimeSubagentState) -> str:
    """Pick the best available transcript snippet to attach to the interrupted-subagent record — preferring a saved report summary, then a freshly summarised execution trace, falling back to a fixed string so the journal entry is never empty."""
    report = load_subagent_report(Workspace.from_path(root), task.id, active.id)
    if report:
        summary = str(report.get("summary") or "").strip()
        if summary:
            return summary
    ref = next((candidate for candidate in reversed(task.subagents) if candidate.id == active.id), None)
    if ref is None:
        return active.execution_trace_snippet or "runner interrupted before subagent completion"
    trace = load_subagent_execution_trace(root, task, ref, active=True, runtime_state=active)
    if trace is None:
        snippet = ""
    else:
        snippet = summarize_transcript(trace.text)
    if snippet:
        return snippet
    return active.execution_trace_snippet or "runner interrupted before subagent completion"


def _interrupted_subagent_reason(task: TaskRecord, reason: str) -> str:
    """Preserve a previously-recorded interruption reason on re-interruption of the same subagent so a second repair pass doesn't overwrite the original cause with a generic "stale runner" label."""
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
    root: Path,
    task: TaskRecord,
    subagent: RuntimeSubagentState,
    resume_stage: str,
) -> None:
    """Persist the interrupted subagent's session+report files in-place so disk artifacts and the in-memory task record agree; the resume flow reads these to decide whether to continue or re-run the subagent."""
    now = utcnow()
    workspace = Workspace.from_path(root)
    session_payload = load_subagent_session(workspace, task.id, subagent.id)
    report_payload = load_subagent_report(workspace, task.id, subagent.id)
    if subagent.continuation is None:
        continuation_payload = None
    else:
        continuation_payload = subagent.continuation.model_dump(mode="python")
    session_payload.update(
        {
            "id": subagent.id,
            "role": subagent.role,
            "engine": subagent.engine,
            "status": subagent.status,
            "sandboxed": subagent.sandboxed,
            "sandbox": subagent.sandbox_summary or "host",
            "updated_at": now,
            "pid": subagent.pid,
            "exit_code": subagent.exit_code,
            "interruption_reason": subagent.interruption_reason or None,
            "resume_stage": resume_stage,
            "continuation": continuation_payload,
        }
    )
    report_payload["status"] = subagent.status
    report_payload["summary"] = report_payload.get("summary") or subagent.execution_trace_snippet
    report_payload["interruption_reason"] = subagent.interruption_reason or None
    report_payload["resume_stage"] = resume_stage
    report_payload["continuation"] = None
    if subagent.continuation is not None:
        report_payload["continuation"] = subagent.continuation.model_dump(mode="python")
    save_subagent_artifacts(
        workspace,
        task.id,
        subagent.id,
        session=session_payload,
        report=report_payload,
    )
