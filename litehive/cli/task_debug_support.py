"""Debug command — inspect subagent artifacts for a task."""

from pathlib import Path

from litehive.agents.execution_trace import load_subagent_execution_trace
from litehive.agents.session_store import load_subagent_report, load_subagent_session
from litehive.tasks.paths import (
    read_text_artifact,
    resolve_artifact_path,
    task_dir,
)
from litehive.tasks.activity import latest_task_activity_entry
from litehive.tasks.report_storage import latest_stage_report
from litehive.worktree import WorktreeService


def debug_all(root: Path, task):
    """List all subagents with status summary."""
    if not task.subagents:
        print(f"{task.id}: no subagents")
        return 0

    print(f"{task.id}: {len(task.subagents)} subagent(s)")
    print()
    for ref in task.subagents:
        exit_code = _read_exit_code(root, task.id, ref.id)
        exit_str = str(exit_code) if exit_code is not None else "-"
        print(f"  {ref.id}  role={ref.role}  engine={ref.engine}  status={ref.status}  exit_code={exit_str}")
    return 0


def debug_latest(root: Path, task):
    """Show detailed info for the latest subagent."""
    # Find the latest subagent ref and its artifact directory
    if not task.subagents:
        print(f"{task.id}: no subagents")
        return 0

    ref = task.subagents[-1]
    sa_base = task_dir(root, task) / ref.path

    # -- Session info --
    print(f"task: {task.id}")
    print(f"subagent: {ref.id}")
    print(f"engine: {ref.engine}")
    print(f"role: {ref.role}")
    print(f"status: {ref.status}")

    # Prefer task runtime state loaded from SQLite; fall back to session artifacts when needed.
    runtime_sa = None
    if task.runtime.execution.active_subagent and task.runtime.execution.active_subagent.id == ref.id:
        runtime_sa = task.runtime.execution.active_subagent

    if runtime_sa is not None:
        print(f"exit_code: {runtime_sa.exit_code if runtime_sa.exit_code is not None else '-'}")
        print(f"started_at: {runtime_sa.started_at}")
        if runtime_sa.completed_at:
            print(f"completed_at: {runtime_sa.completed_at}")
    else:
        session_data = load_subagent_session(root, task.id, ref.id)
        if session_data:
            ec = session_data.get("exit_code")
            print(f"exit_code: {ec if ec is not None else '-'}")
            if "created_at" in session_data:
                print(f"created_at: {session_data['created_at']}")
            if "updated_at" in session_data:
                print(f"session_updated_at: {session_data['updated_at']}")
        else:
            print("exit_code: -")

    # -- Verdict from task activity --
    _print_verdict(root, task, ref.role)

    # -- Report summary --
    if sa_base.exists():
        report_data = load_subagent_report(root, task.id, ref.id)
        if report_data.get("verdict"):
            print(f"report_verdict: {report_data['verdict']}")
    stage_report = latest_stage_report(root, task, source="hook")
    if stage_report is not None:
        print(f"stage_report_verdict: {stage_report.verdict}")
        print(f"stage_report_source: {stage_report.source}")
        print(f"stage_report_pipeline_state: {stage_report.pipeline_state}")
        print(f"stage_report_summary: {stage_report.summary}")

    # -- Execution trace summary (first 200 chars) --
    if sa_base.exists():
        _print_execution_trace(root, task, ref, runtime_sa)

    # -- stdout tail (last 500 chars) --
    if sa_base.exists():
        _print_stream_tail(sa_base, "stdout.txt", "stdout")

    # -- stderr tail (last 500 chars) --
    if sa_base.exists():
        _print_stream_tail(sa_base, "stderr.txt", "stderr")

    return 0


def debug_worktree(root: Path, task):
    """Show whether the task worktree exists and what changed inside it."""
    inspection = WorktreeService(root).inspect_task_worktree(task)
    print(f"task: {task.id}")
    if not inspection.worktree_rel:
        print("worktree: no worktree")
        return 0

    if not inspection.exists:
        print(f"worktree: {inspection.worktree_rel}")
        print("exists: no")
        print("no worktree")
        return 0

    print(f"worktree: {inspection.worktree_rel}")
    print("exists: yes")

    _print_path_list("uncommitted", inspection.uncommitted)
    _print_path_list("committed_ahead_of_main", inspection.committed_ahead_of_main)
    return 0


def _read_exit_code(root: Path, task_id: str, subagent_id: str) -> int | None:
    """Read exit_code from runtime/session storage for a subagent."""
    session = load_subagent_session(root, task_id, subagent_id)
    value = session.get("exit_code")
    return value if isinstance(value, int) else None


def _print_verdict(root, task, role):
    """Cross-reference task activity for the latest non-comment verdict matching the role."""
    verdict_entry = latest_task_activity_entry(
        root,
        task,
        role=role,
        verdicts={"pass", "reject", "blocked"},
    )

    if verdict_entry is not None:
        print(f"verdict: {verdict_entry.verdict}")
        print(f"verdict_stage: {verdict_entry.stage}")
        # Show first line of verdict message
        first_line = verdict_entry.message.split("\n", 1)[0][:120]
        print(f"verdict_message: {first_line}")
    else:
        print("verdict: none")


def _print_execution_trace(root, task, ref, runtime_sa):
    """Print first 200 chars of the execution trace."""
    is_active = bool(task.runtime.execution.active_subagent and task.runtime.execution.active_subagent.id == ref.id)
    trace = load_subagent_execution_trace(
        root,
        task,
        ref,
        active=is_active,
        runtime_state=runtime_sa,
    )
    if trace is None:
        print("execution trace: (not found)")
        return
    try:
        content = trace.text
        total_len = len(content)
        preview = content[:200]
        if total_len > 200:
            print(f"execution trace ({total_len} chars, showing first 200):")
        else:
            print(f"execution trace ({total_len} chars):")
        print(f"  {preview}")
    except Exception:
        print("execution trace: (error reading)")


def _print_stream_tail(sa_base, filename, label):
    """Print last 500 chars of a stream artifact."""
    path = resolve_artifact_path(sa_base, filename)
    if path is None:
        print(f"{label}: (not found)")
        return
    try:
        content = read_text_artifact(path)
        total_len = len(content)
        if total_len == 0:
            print(f"{label}: (empty)")
            return
        tail = content[-500:]
        if total_len > 500:
            print(f"{label} (last 500 of {total_len} chars):")
            print(f"  ...{tail}")
        else:
            print(f"{label} ({total_len} chars):")
            print(f"  {tail}")
    except Exception:
        print(f"{label}: (error reading)")


def _print_path_list(label: str, paths: list[str]) -> None:
    print(f"{label}:")
    if not paths:
        print("  (none)")
        return
    for path in paths:
        print(f"  - {path}")
