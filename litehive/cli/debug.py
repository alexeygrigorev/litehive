"""CLI handler for the debug command — inspect subagent artifacts for a task."""

from pathlib import Path

import yaml

from litehive.cli.report import _resolve_workspace_root
from litehive.tasks import (
    _latest_subagent_base,
    _read_text_artifact,
    _resolve_artifact_path,
    get_task,
    load_task_thread,
    task_dir,
)


def _cmd_debug(args):
    root = _resolve_workspace_root(args.workspace)
    task = get_task(root, args.task_id)
    if task is None:
        print(f"task {args.task_id} not found")
        return 1

    if args.show_all:
        return _debug_all(root, task)
    return _debug_latest(root, task)


def _debug_all(root, task):
    if not task.subagents:
        print(f"task {task.id}: no subagents")
        return 0
    print(f"task: {task.id}")
    print(f"subagents: {len(task.subagents)}")
    print()
    for ref in task.subagents:
        base = task_dir(root, task) / ref.path
        exit_code = _read_exit_code(base)
        exit_str = str(exit_code) if exit_code is not None else "-"
        print(f"  {ref.id}  role={ref.role}  engine={ref.engine}  status={ref.status}  exit_code={exit_str}")
    return 0


def _debug_latest(root, task):
    base = _latest_subagent_base(root, task)
    if base is None:
        print(f"task {task.id}: no subagent artifacts found")
        return 1

    session = _read_session(base)
    print(f"task: {task.id}")
    print(f"subagent: {base.name}")
    print()

    # Session info
    print("── session ──")
    print(f"  engine:    {session.get('engine', '-')}")
    print(f"  role:      {session.get('role', '-')}")
    print(f"  status:    {session.get('status', '-')}")
    print(f"  exit_code: {session.get('exit_code', '-')}")
    created = session.get("created_at", "")
    updated = session.get("updated_at", "")
    if created and updated:
        print(f"  created:   {created}")
        print(f"  updated:   {updated}")
    print()

    # Verdict from thread
    verdict_info = _find_verdict(root, task, session)
    print("── verdict ──")
    if verdict_info:
        print(f"  submitted: yes")
        print(f"  verdict:   {verdict_info['verdict']}")
        print(f"  role:      {verdict_info['role']}")
        print(f"  step:      {verdict_info['step']}")
    else:
        print(f"  submitted: no")
    print()

    # Transcript summary
    transcript_path = _resolve_artifact_path(base, "transcript.md")
    print("── transcript ──")
    if transcript_path is not None:
        text = _read_text_artifact(transcript_path)
        snippet = text[:200].strip()
        if snippet:
            print(f"  {snippet}")
            if len(text) > 200:
                print(f"  ... ({len(text)} chars total)")
        else:
            print("  (empty)")
    else:
        print("  (not found)")
    print()

    # stdout tail
    stdout_path = _resolve_artifact_path(base, "stdout.txt")
    print("── stdout (last 500 chars) ──")
    if stdout_path is not None:
        text = _read_text_artifact(stdout_path)
        tail = text[-500:].strip()
        if tail:
            if len(text) > 500:
                print("  ...")
            for line in tail.splitlines():
                print(f"  {line}")
        else:
            print("  (empty)")
    else:
        print("  (not found)")
    print()

    # stderr tail
    stderr_path = _resolve_artifact_path(base, "stderr.txt")
    print("── stderr (last 500 chars) ──")
    if stderr_path is not None:
        text = _read_text_artifact(stderr_path)
        tail = text[-500:].strip()
        if tail:
            if len(text) > 500:
                print("  ...")
            for line in tail.splitlines():
                print(f"  {line}")
        else:
            print("  (empty)")
    else:
        print("  (not found)")

    return 0


def _read_session(base: Path) -> dict:
    session_path = _resolve_artifact_path(base, "session.yaml")
    if session_path is None:
        return {}
    text = _read_text_artifact(session_path)
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def _read_exit_code(base: Path) -> int | None:
    session = _read_session(base)
    return session.get("exit_code")


def _find_verdict(root, task, session: dict):
    """Find the most recent thread verdict that matches this subagent's role."""
    thread = load_task_thread(root, task)
    if not thread:
        return None
    role = session.get("role", "")
    # Find the last non-comment verdict from the matching role
    for comment in reversed(thread):
        if comment.verdict != "comment" and comment.role == role:
            return {
                "verdict": comment.verdict,
                "role": comment.role,
                "step": comment.step,
            }
    return None
