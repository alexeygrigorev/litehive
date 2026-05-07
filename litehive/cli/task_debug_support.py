"""
Minimal task evidence rendering for operators and recovery.

Produces the compact ``task evidence``/``task debug`` views used to
triage a stuck task: lifecycle state, latest stage report, latest
activity entry, latest subagent run, and worktree summary. The
output is line-oriented so it pipes cleanly into a follow-up grep
or another script.
"""

from pathlib import Path
import sqlite3

from litehive.agents.execution_trace import load_subagent_execution_trace
from litehive.tasks.paths import (
    read_text_artifact,
    resolve_artifact_path,
)
from litehive.tasks.report_storage import latest_stage_report
from litehive.workspace import Workspace
from litehive.worktree.service import WorktreeService


def render_task_evidence_for_workspace(workspace: Workspace, task) -> int:
    """
    Render compact task evidence from an injected workspace.
    """
    print(f"task: {task.id}")
    print(f"title: {task.title}")
    print(f"status: {task.status}")
    print(f"pipeline_status: {task.pipeline_status}")
    _print_lifecycle_evidence(workspace, task)
    _print_latest_report(workspace, task)
    _print_latest_activity(workspace, task)
    _print_latest_subagent(workspace, task)
    _print_worktree_evidence(workspace, task)
    return 0


def debug_all_for_workspace(workspace: Workspace, task):
    """
    List every subagent attached to a task using an injected workspace.
    """
    if not task.subagents:
        print(f"{task.id}: no subagents")
        return 0

    print(f"{task.id}: {len(task.subagents)} subagent(s)")
    print()
    for ref in task.subagents:
        exit_code = _read_exit_code(workspace, task.id, ref.id)
        if exit_code is not None:
            exit_str = str(exit_code)
        else:
            exit_str = "-"
        print(f"  {ref.id}  role={ref.role}  engine={ref.engine}  status={ref.status}  exit_code={exit_str}")
    return 0


def debug_latest_for_workspace(workspace: Workspace, task):
    """
    Compatibility entrypoint for the compact task evidence view.

    Routes the older ``task debug`` (no flags) muscle memory to
    :func:`render_task_evidence_for_workspace`. Kept as a thin wrapper so the
    public Typer signature in ``task_cli`` does not change.
    """
    return render_task_evidence_for_workspace(workspace, task)


def _print_lifecycle_evidence(workspace: Workspace, task) -> None:
    """
    Print the lifecycle-state slice of the evidence view.

    Renders stage, failure reason and message, active recovery
    trigger (origin, kind, fingerprint, classification), and the
    last recovery outcome. Lets an operator triaging a stuck task
    see what the state machine thinks the task is doing without
    opening the database.
    """
    from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound  # noqa: PLC0415

    try:
        state = SqlitePersistence(workspace).load(task.id)
    except TaskNotFound:
        print("lifecycle_state: not found")
        return
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        print(f"lifecycle_state: unavailable ({type(exc).__name__}: {exc})")
        return

    print(f"lifecycle_stage: {state.stage}")
    print(f"failed_reason: {_enum_value(state.failed_reason) or '-'}")
    print(f"failed_message: {state.failed_message or '-'}")
    print(f"recovery_failure_explanation: {state.recovery_failure_explanation or '-'}")
    trigger = state.active_recovery_trigger
    if trigger is None:
        print("active_recovery_trigger: none")
    else:
        fingerprint = trigger.failure_fingerprint
        print(
            "active_recovery_trigger: "
            f"origin_stage={trigger.origin_stage or '-'} "
            f"kind={trigger.trigger_event_kind.value} "
            f"reason_code={trigger.reason_code or '-'} "
            f"fingerprint={fingerprint.fingerprint} "
            f"classification={fingerprint.classification or '-'}"
        )
        if trigger.message:
            print(f"active_recovery_message: {_first_line(trigger.message)}")
    print(f"recovery_history_count: {len(state.recovery_history)}")
    if state.recovery_history:
        outcome = state.recovery_history[-1]
        print(
            "latest_recovery: "
            f"verdict={outcome.recovery_verdict or '-'} "
            f"disposition={outcome.disposition.value} "
            f"origin_stage={outcome.trigger.origin_stage or '-'} "
            f"fingerprint={outcome.trigger.failure_fingerprint.fingerprint}"
        )


def _print_latest_report(workspace: Workspace, task) -> None:
    """
    Print the most recent stage report, summarized to one line.

    Shows stage, verdict, source, and the first line of the
    summary so the operator can confirm what the last agent
    submitted without opening the report file. Part of the
    compact evidence view so the entire triage screen stays
    readable.
    """
    report = latest_stage_report(workspace, task)
    if report is None:
        print("latest_stage_report: none")
        return
    print(
        "latest_stage_report: "
        f"stage={report.pipeline_state} verdict={report.verdict} source={report.source} "
        f"summary={_first_line(report.summary)}"
    )


def _print_latest_activity(workspace: Workspace, task) -> None:
    """
    Print the last entry from the task activity log.

    Independent of stage reports because activity records every
    agent verdict (including ``comment`` entries that never become
    stage reports). Showing both surfaces in evidence covers the
    case where an agent posted feedback without a verdict change.
    """
    entry = workspace.task_activity(task).latest()
    if entry is None:
        print("latest_activity: none")
        return
    print(
        "latest_activity: "
        f"stage={entry.stage} role={entry.role} verdict={entry.verdict} "
        f"message={_first_line(entry.message)}"
    )


def _print_latest_subagent(workspace: Workspace, task) -> None:
    """
    Print the most recent subagent's identity and execution evidence.

    Surfaces id, role, engine, status, exit code, timestamps, and
    whether any output was produced — the evidence view's primary
    signal for "did the engine actually run?" when triaging stuck
    or empty tasks. Falls back to the persisted session row when
    the runtime view does not have the subagent loaded.
    """
    if not task.subagents:
        print("latest_subagent: none")
        return

    ref = task.subagents[-1]
    sa_base = workspace.task_dir(task) / ref.path
    runtime_sa = None
    if task.runtime.execution.active_subagent and task.runtime.execution.active_subagent.id == ref.id:
        runtime_sa = task.runtime.execution.active_subagent

    exit_code = None
    started_at = None
    completed_at = None
    if runtime_sa is not None:
        exit_code = runtime_sa.exit_code
        started_at = runtime_sa.started_at
        completed_at = runtime_sa.completed_at
    else:
        session = workspace.load_subagent_session_record(task.id, ref.id)
        if session:
            exit_code = session.exit_code
            started_at = session.created_at
            completed_at = session.updated_at

    produced_output = False
    trace = None
    if sa_base.exists():
        is_active = bool(task.runtime.execution.active_subagent and task.runtime.execution.active_subagent.id == ref.id)
        trace = load_subagent_execution_trace(workspace, task, ref, active=is_active, runtime_state=runtime_sa)
        produced_output = trace is not None and bool(trace.text.strip())
        for filename in ("stdout.txt", "stdout.log", "stderr.txt", "stderr.log"):
            path = resolve_artifact_path(sa_base, filename)
            if path is not None and read_text_artifact(path).strip():
                produced_output = True
                break

    if exit_code is not None:
        exit_code_label = exit_code
    else:
        exit_code_label = "-"
    if produced_output:
        produced_output_label = "yes"
    else:
        produced_output_label = "no"
    print(
        "latest_subagent: "
        f"id={ref.id} role={ref.role} engine={ref.engine} status={ref.status} "
        f"exit_code={exit_code_label} "
        f"started_at={started_at or '-'} completed_at={completed_at or '-'} "
        f"produced_output={produced_output_label}"
    )
    if trace is not None and isinstance(trace.source, Path):
        print(f"latest_subagent_trace_source: {trace.source.relative_to(workspace.root)}")


def debug_worktree_for_workspace(workspace: Workspace, task):
    """
    Render the worktree-only evidence view from an injected workspace.
    """
    print(f"task: {task.id}")
    _print_worktree_evidence(workspace, task)
    return 0


def _print_worktree_evidence(workspace: Workspace, task) -> None:
    """
    Print the task's worktree state.

    Reports existence, uncommitted file count, and the count of
    files committed-ahead-of-main; both file lists are compacted
    so a task with hundreds of changes does not flood the view.
    Used by both the full evidence view and ``debug_worktree`` so
    the operator can tell at a glance whether the task has any
    uncommitted work.
    """
    inspection = WorktreeService(workspace).inspect_task_worktree(task)
    if not inspection.worktree_rel:
        print("worktree: none")
        return

    if not inspection.exists:
        print(f"worktree: {inspection.worktree_rel} exists=no")
        return

    print(
        f"worktree: {inspection.worktree_rel} exists=yes "
        f"uncommitted={len(inspection.uncommitted)} committed_ahead_of_main={len(inspection.committed_ahead_of_main)}"
    )
    if inspection.uncommitted:
        print(f"worktree_uncommitted: {_compact_paths(inspection.uncommitted)}")
    if inspection.committed_ahead_of_main:
        print(f"worktree_committed_ahead_of_main: {_compact_paths(inspection.committed_ahead_of_main)}")


def _read_exit_code(workspace: Workspace, task_id: str, subagent_id: str) -> int | None:
    """
    Read a subagent's exit code from the persisted session row.

    Used by ``debug_all`` so each subagent line carries the exit
    code without the caller having to load every session row in
    the same format. Returns ``None`` when the value is missing
    or non-integer so the caller can render ``-`` instead of a
    fake zero.
    """
    return workspace.load_subagent_session_record(task_id, subagent_id).exit_code


def _enum_value(value) -> str | None:
    """
    Return ``value.value`` for enum members, ``str(value)`` otherwise.

    ``_print_lifecycle_evidence`` uses this so a ``FailedReason``
    enum and any legacy bare-string representation render
    identically in the evidence output. Returns ``None`` for
    ``None`` so the caller can substitute the dash sentinel.
    """
    if value is None:
        return None
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _first_line(value: str, limit: int = 180) -> str:
    """
    Compress a multi-line string to its first line, truncated with an ellipsis.

    The evidence view uses this so summary and message fields
    stay one screen line even when the underlying text spans
    paragraphs. Empty input renders as ``-`` so the cell is
    never silently blank.
    """
    if value.strip():
        text = value.strip().splitlines()[0]
    else:
        text = "-"
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _compact_paths(paths: list[str], limit: int = 6) -> str:
    """
    Render a long path list as the first ``limit`` entries plus an overflow count.

    Used by the worktree evidence printer so a task with hundreds
    of changed files does not flood the evidence output. Six is
    enough to spot the kind of files involved without scrolling
    past the rest of the evidence.
    """
    shown = paths[:limit]
    if len(paths) <= limit:
        suffix = ""
    else:
        suffix = f", ... (+{len(paths) - limit})"
    return ", ".join(shown) + suffix
