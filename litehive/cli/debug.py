"""Debug command — inspect subagent artifacts for a task."""

from pathlib import Path
import subprocess

import yaml

from litehive.config import ensure_workspace
from litehive.git.ops import current_head
from litehive.tasks.crud import get_task_worktree_path, require_task, save_task
from litehive.tasks.paths import (
    read_text_artifact,
    resolve_artifact_path,
    task_dir,
)
from litehive.tasks.reports import load_task_thread
from litehive.tasks.worktrees import migrate_legacy_worktree


def cmd_debug(args):
    ensure_workspace(args.workspace)
    try:
        task = require_task(args.workspace, args.task_id)
    except ValueError:
        print(f"task not found: {args.task_id}")
        return 1

    if getattr(args, "worktree", False):
        return _debug_worktree(args.workspace, task)
    if args.all:
        return _debug_all(args.workspace, task)
    return _debug_latest(args.workspace, task)


def _debug_all(root: Path, task):
    """List all subagents with status summary."""
    if not task.subagents:
        print(f"{task.id}: no subagents")
        return 0

    print(f"{task.id}: {len(task.subagents)} subagent(s)")
    print()
    for ref in task.subagents:
        base = task_dir(root, task) / ref.path
        exit_code = _read_exit_code(base)
        exit_str = str(exit_code) if exit_code is not None else "-"
        print(f"  {ref.id}  role={ref.role}  engine={ref.engine}  status={ref.status}  exit_code={exit_str}")
    return 0


def _debug_latest(root: Path, task):
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

    # Try to get richer runtime state (exit_code, timing) from runtime.yaml
    runtime_sa = None
    if task.runtime.last_subagent and task.runtime.last_subagent.id == ref.id:
        runtime_sa = task.runtime.last_subagent
    elif task.runtime.active_subagent and task.runtime.active_subagent.id == ref.id:
        runtime_sa = task.runtime.active_subagent

    if runtime_sa is not None:
        print(f"exit_code: {runtime_sa.exit_code if runtime_sa.exit_code is not None else '-'}")
        print(f"started_at: {runtime_sa.started_at}")
        if runtime_sa.completed_at:
            print(f"completed_at: {runtime_sa.completed_at}")
    else:
        # Fall back to session.yaml if it exists
        session_path = resolve_artifact_path(sa_base, "session.yaml") if sa_base.exists() else None
        if session_path is not None:
            try:
                session_text = read_text_artifact(session_path)
                session_data = yaml.safe_load(session_text)
                if isinstance(session_data, dict):
                    ec = session_data.get("exit_code")
                    print(f"exit_code: {ec if ec is not None else '-'}")
                    if "created_at" in session_data:
                        print(f"created_at: {session_data['created_at']}")
                    if "updated_at" in session_data:
                        print(f"session_updated_at: {session_data['updated_at']}")
            except Exception:
                print("exit_code: -")
        else:
            print("exit_code: -")

    # -- Verdict from thread --
    _print_verdict(root, task, ref.role)

    # -- Report summary --
    if sa_base.exists():
        report_path = resolve_artifact_path(sa_base, "report.yaml")
        if report_path is not None:
            try:
                report_text = read_text_artifact(report_path)
                report_data = yaml.safe_load(report_text)
                if isinstance(report_data, dict) and report_data.get("verdict"):
                    print(f"report_verdict: {report_data['verdict']}")
            except Exception:
                pass

    # -- Transcript summary (first 200 chars) --
    if sa_base.exists():
        _print_transcript(sa_base)

    # -- stdout tail (last 500 chars) --
    if sa_base.exists():
        _print_stream_tail(sa_base, "stdout.txt", "stdout")

    # -- stderr tail (last 500 chars) --
    if sa_base.exists():
        _print_stream_tail(sa_base, "stderr.txt", "stderr")

    return 0


def _debug_worktree(root: Path, task):
    """Show whether the task worktree exists and what changed inside it."""
    worktree_rel = get_task_worktree_path(task)
    print(f"task: {task.id}")
    if not worktree_rel:
        print("worktree: no worktree")
        return 0

    worktree_path, changed = migrate_legacy_worktree(root, task)
    if changed:
        save_task(root, task)
        worktree_rel = get_task_worktree_path(task) or worktree_rel
    if worktree_path is None or not worktree_path.exists():
        print(f"worktree: {worktree_rel}")
        print("exists: no")
        print("no worktree")
        return 0

    print(f"worktree: {worktree_rel}")
    print("exists: yes")

    uncommitted = _worktree_uncommitted_changes(worktree_path)
    committed = _worktree_committed_changes(root, worktree_path)
    _print_path_list("uncommitted", uncommitted)
    _print_path_list("committed_ahead_of_main", committed)
    return 0


def _read_exit_code(base: Path) -> int | None:
    """Read exit_code from session.yaml in a subagent artifact directory."""
    session_path = resolve_artifact_path(base, "session.yaml") if base.exists() else None
    if session_path is None:
        return None
    try:
        text = read_text_artifact(session_path)
        data = yaml.safe_load(text)
        return data.get("exit_code") if isinstance(data, dict) else None
    except Exception:
        return None


def _print_verdict(root, task, role):
    """Cross-reference task comments for the latest non-comment verdict matching the role."""
    thread = load_task_thread(root, task)
    verdict_entry = None
    for entry in reversed(thread):
        if entry.role == role and entry.verdict != "comment":
            verdict_entry = entry
            break

    if verdict_entry is not None:
        print(f"verdict: {verdict_entry.verdict}")
        print(f"verdict_step: {verdict_entry.step}")
        # Show first line of verdict message
        first_line = verdict_entry.message.split("\n", 1)[0][:120]
        print(f"verdict_message: {first_line}")
    else:
        print("verdict: none")


def _print_transcript(sa_base):
    """Print first 200 chars of the transcript."""
    path = resolve_artifact_path(sa_base, "transcript.md")
    if path is None:
        print("transcript: (not found)")
        return
    try:
        content = read_text_artifact(path)
        total_len = len(content)
        preview = content[:200]
        if total_len > 200:
            print(f"transcript ({total_len} chars, showing first 200):")
        else:
            print(f"transcript ({total_len} chars):")
        print(f"  {preview}")
    except Exception:
        print("transcript: (error reading)")


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


def _worktree_uncommitted_changes(worktree_path: Path) -> list[str]:
    try:
        status_result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if status_result.returncode != 0:
        return []
    return _status_lines_to_paths(status_result.stdout.splitlines())


def _worktree_committed_changes(root: Path, worktree_path: Path) -> list[str]:
    main_head = current_head(root) or "HEAD"
    try:
        merge_base_result = subprocess.run(
            ["git", "merge-base", main_head, "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if merge_base_result.returncode != 0:
        return []

    fork_point = merge_base_result.stdout.strip()
    if not fork_point:
        return []

    try:
        committed = subprocess.run(
            ["git", "diff", "--name-only", fork_point, "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if committed.returncode != 0:
        return []
    return sorted({line.strip() for line in committed.stdout.splitlines() if line.strip()})


def _status_lines_to_paths(lines: list[str]) -> list[str]:
    paths: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) > 3 else ""
        if not path:
            continue
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        paths.add(path)
    return sorted(paths)
