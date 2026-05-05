"""Minimal task evidence rendering for operators and recovery."""

from pathlib import Path
import sqlite3

from litehive.agents.execution_trace import load_subagent_execution_trace
from litehive.agents.session_store import load_subagent_session
from litehive.tasks.paths import (
    read_text_artifact,
    resolve_artifact_path,
    task_dir,
)
from litehive.tasks.activity import load_task_activity
from litehive.tasks.report_storage import latest_stage_report
from litehive.workspace import Workspace
from litehive.worktree import WorktreeService


def render_task_evidence(root: Path, task) -> int:
    """Render the compact evidence needed to route or recover a task."""
    print(f"task: {task.id}")
    print(f"title: {task.title}")
    print(f"status: {task.status}")
    print(f"pipeline_status: {task.pipeline_status}")
    _print_lifecycle_evidence(root, task)
    _print_latest_report(root, task)
    _print_latest_activity(root, task)
    _print_latest_subagent(root, task)
    _print_worktree_evidence(root, task)
    return 0


def debug_all(root: Path, task):
    """List all subagents with status summary."""
    if not task.subagents:
        print(f"{task.id}: no subagents")
        return 0

    print(f"{task.id}: {len(task.subagents)} subagent(s)")
    print()
    for ref in task.subagents:
        exit_code = _read_exit_code(root, task.id, ref.id)
        if exit_code is not None:
            exit_str = str(exit_code)
        else:
            exit_str = "-"
        print(f"  {ref.id}  role={ref.role}  engine={ref.engine}  status={ref.status}  exit_code={exit_str}")
    return 0


def debug_latest(root: Path, task):
    """Compatibility entrypoint for the compact task evidence view."""
    return render_task_evidence(root, task)


def _print_lifecycle_evidence(root: Path, task) -> None:
    """Print the lifecycle-state slice of the evidence view (stage, failed reason, active recovery trigger, recovery history); ``render_task_evidence`` calls this so an operator triaging a stuck task can see what the state machine thinks the task is doing."""
    try:
        from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound  # noqa: PLC0415

        state = SqlitePersistence(Workspace.from_path(root)).load(task.id)
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


def _print_latest_report(root: Path, task) -> None:
    """Print the most recent ``StageReport`` (stage, verdict, source, summary first line); part of the compact evidence view so the operator can confirm what the last agent submitted without opening the report file."""
    report = latest_stage_report(Workspace.from_path(root), task)
    if report is None:
        print("latest_stage_report: none")
        return
    print(
        "latest_stage_report: "
        f"stage={report.pipeline_state} verdict={report.verdict} source={report.source} "
        f"summary={_first_line(report.summary)}"
    )


def _print_latest_activity(root: Path, task) -> None:
    """Print the last task_activity entry (stage, role, verdict, message first line); part of the evidence view so the operator can see the latest agent verdict regardless of whether a stage report was also written."""
    activity = load_task_activity(Workspace.from_path(root), task)
    if not activity:
        print("latest_activity: none")
        return
    entry = activity[-1]
    print(
        "latest_activity: "
        f"stage={entry.stage} role={entry.role} verdict={entry.verdict} "
        f"message={_first_line(entry.message)}"
    )


def _print_latest_subagent(root: Path, task) -> None:
    """Print the most recent subagent's identity, exit code, timestamps, and whether it produced any output; the evidence view's primary signal for ``did the engine actually run`` when triaging stuck or empty tasks."""
    if not task.subagents:
        print("latest_subagent: none")
        return

    ref = task.subagents[-1]
    sa_base = task_dir(root, task) / ref.path
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
        session_data = load_subagent_session(Workspace.from_path(root), task.id, ref.id)
        if session_data:
            exit_code = session_data.get("exit_code")
            started_at = session_data.get("created_at")
            completed_at = session_data.get("updated_at")

    produced_output = False
    trace = None
    if sa_base.exists():
        is_active = bool(task.runtime.execution.active_subagent and task.runtime.execution.active_subagent.id == ref.id)
        trace = load_subagent_execution_trace(root, task, ref, active=is_active, runtime_state=runtime_sa)
        produced_output = trace is not None and bool(trace.text.strip())
        for filename in ("stdout.txt", "stdout.log", "stderr.txt", "stderr.log"):
            path = resolve_artifact_path(sa_base, filename)
            if path is not None and read_text_artifact(path).strip():
                produced_output = True
                break

    print(
        "latest_subagent: "
        f"id={ref.id} role={ref.role} engine={ref.engine} status={ref.status} "
        f"exit_code={exit_code if exit_code is not None else '-'} "
        f"started_at={started_at or '-'} completed_at={completed_at or '-'} "
        f"produced_output={'yes' if produced_output else 'no'}"
    )
    if trace is not None:
        print(f"latest_subagent_trace_source: {trace.source.relative_to(root)}")


def debug_worktree(root: Path, task):
    """Compatibility entrypoint for worktree-only evidence."""
    print(f"task: {task.id}")
    _print_worktree_evidence(root, task)
    return 0


def _print_worktree_evidence(root: Path, task) -> None:
    """Print the task's worktree state (existence, uncommitted file count, committed-ahead-of-main file count); used by both the full evidence view and ``debug_worktree`` so the operator can tell at a glance whether the task has uncommitted work."""
    inspection = WorktreeService(root).inspect_task_worktree(task)
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


def _read_exit_code(root: Path, task_id: str, subagent_id: str) -> int | None:
    """Read exit_code from runtime/session storage for a subagent."""
    session = load_subagent_session(Workspace.from_path(root), task_id, subagent_id)
    value = session.get("exit_code")
    if isinstance(value, int):
        return value
    return None


def _enum_value(value) -> str | None:
    """Return the underlying ``.value`` of an enum or ``str(value)`` of anything else; ``_print_lifecycle_evidence`` uses it so a ``FailedReason`` enum and a legacy bare string both render identically in evidence output."""
    if value is None:
        return None
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _first_line(value: str, limit: int = 180) -> str:
    """Take the first non-empty line of a multi-line message and truncate it with an ellipsis; the evidence view uses this so summary/message fields stay one screen line even when the underlying text is paragraphs long."""
    if value.strip():
        text = value.strip().splitlines()[0]
    else:
        text = "-"
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _compact_paths(paths: list[str], limit: int = 6) -> str:
    """Render a long path list as the first ``limit`` entries plus an overflow count; used by the worktree evidence printer so a task with hundreds of changed files does not flood the evidence output."""
    shown = paths[:limit]
    if len(paths) <= limit:
        suffix = ""
    else:
        suffix = f", ... (+{len(paths) - limit})"
    return ", ".join(shown) + suffix
