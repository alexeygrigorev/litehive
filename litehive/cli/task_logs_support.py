"""
Logs command implementations for daemon, task, and subagent artifacts.

Owns the rendering paths behind ``litehive task logs``: tail a daemon
run, list pool sessions, render the per-task journal, follow a live
subagent's stdout, or list every subagent on a task. Kept as a
support module so the Typer command in ``task_cli`` stays a thin
dispatcher and these helpers can be exercised directly by tests.
"""

from datetime import datetime
from pathlib import Path
import time

from litehive.cli.task_debug_support import render_task_evidence_for_workspace
from litehive.daemon.logs import latest_run_all_log_dir_for_workspace
from litehive.domain.task import TaskRecord
from litehive.tasks.journal import render_task_journal
from litehive.tasks.paths import read_text_artifact, resolve_artifact_path, task_dir
from litehive.workspace import Workspace

_DEFAULT_TAIL_LINES = 40
FOLLOW_POLL_SECONDS = 0.1


def show_latest_daemon_log_for_workspace(workspace: Workspace) -> int:
    """
    Tail the most recent daemon run log.

    Operator default for ``task logs`` without a task id; lets the
    operator see what the pool just did without having to specify
    a session timestamp. Tails the last 40 lines so the output
    fits a terminal page even when the underlying log is large.
    """
    latest_dir = latest_run_all_log_dir_for_workspace(workspace)
    log_path = _latest_daemon_log_path(latest_dir)
    if log_path is None:
        print("No daemon run logs found.")
        return 0
    print(f"daemon log: {log_path}")
    print()
    print(_tail_text(read_text_artifact(log_path)))
    return 0


def list_daemon_sessions_for_workspace(workspace: Workspace) -> int:
    """
    List recent pool sessions with their stop reasons.

    Capped at five so the listing stays scannable when many runs
    accumulate; the operator can ``ls`` the logs directory
    directly when they need history beyond that. Each row carries
    the parsed ISO timestamp so log directories can be sorted by
    eye instead of by the compact ``YYYYmmddTHHMMSSZ`` form.
    """
    logs_root = workspace.runtime_path("logs", "run-all")
    if not logs_root.exists():
        print("No daemon run logs found.")
        return 0

    directories = sorted((path for path in logs_root.iterdir() if path.is_dir()), reverse=True)[:5]
    if not directories:
        print("No daemon run logs found.")
        return 0

    for directory in directories:
        print(
            f"{directory.name}  timestamp={_format_session_timestamp(directory.name)}  "
            f"outcome={_session_outcome(directory)}"
        )
    return 0


def show_task_journal_for_workspace(workspace: Workspace, task: TaskRecord) -> int:
    """
    Render a task journal from an injected workspace.

    Kept separate from the path wrapper so CLI callers that already
    resolved workspace dependencies do not rebuild them for display.
    """
    journal = render_task_journal(workspace, task)
    if not journal:
        print(f"{task.id}: journal not found")
        return 0
    print(journal)
    return 0


def show_latest_subagent_for_workspace(workspace: Workspace, task: TaskRecord) -> int:
    """
    One-screen view of the most recent subagent run for a task.

    Routes to the same evidence renderer as ``task evidence`` so
    both surfaces stay consistent — operators reach the same
    triage screen whether they enter via ``task logs --agent`` or
    ``task evidence`` and don't have to learn two different
    layouts.
    """
    return render_task_evidence_for_workspace(workspace, task)


def list_task_subagents_for_workspace(workspace: Workspace, task: TaskRecord) -> int:
    """
    List task subagents from an injected workspace.
    """
    if not task.subagents:
        print(f"{task.id}: no subagents")
        return 0

    runtime_by_id = {}
    if task.runtime.execution.active_subagent is not None:
        runtime_by_id[task.runtime.execution.active_subagent.id] = task.runtime.execution.active_subagent

    for ref in reversed(task.subagents):
        runtime_state = runtime_by_id.get(ref.id)
        session = workspace.load_subagent_session_record(task.id, ref.id)
        exit_code = _pick_runtime_value(runtime_state, "exit_code")
        if exit_code is None:
            exit_code = session.exit_code
        started_at = _pick_runtime_value(runtime_state, "started_at") or session.created_at
        completed_at = _pick_runtime_value(runtime_state, "completed_at") or session.updated_at
        duration = _format_duration(started_at, completed_at)
        if exit_code is not None:
            exit_str = str(exit_code)
        else:
            exit_str = "-"
        print(
            f"{ref.id}  role={ref.role}  engine={ref.engine}  status={ref.status}  "
            f"exit_code={exit_str}  duration={duration}"
        )
    return 0


def follow_active_subagent_for_workspace(workspace: Workspace, task_id: str | None = None) -> int:
    """
    ``tail -f`` analogue for the live subagent stdout.

    Lets the operator watch a running stage without leaving the
    CLI. Returns immediately when there is nothing to follow so
    the command does not hang on an idle workspace. Polling at
    :data:`FOLLOW_POLL_SECONDS` is fine because the subagent's
    own writer is not synchronous-flushed; a tighter poll just
    burns CPU.
    """
    task = resolve_follow_task_for_workspace(workspace, task_id=task_id)
    if task is None:
        ref = None
    else:
        ref = _latest_subagent_ref(task)
    is_active = bool(
        task is not None
        and ref is not None
        and task.runtime.execution.active_subagent is not None
        and task.runtime.execution.active_subagent.id == ref.id
    )
    if task is None or ref is None:
        print("No active subagent.")
        return 0

    active_task_id = task.id
    active_subagent_id = ref.id
    active_path = ref.path
    base = task_dir(workspace.root, task) / active_path
    stdout_path = _artifact_for_kind(base, "stdout", active=is_active)
    if stdout_path is None:
        print("Active subagent stdout not found.")
        return 0

    print(f"following: {stdout_path.relative_to(workspace.root)}")
    position = 0
    if not is_active:
        _print_follow_chunk(stdout_path, position)
        return 0

    while True:
        position = _print_follow_chunk(stdout_path, position)
        task = resolve_follow_task_for_workspace(workspace, task_id=task_id)
        if task is None or task.runtime.execution.active_subagent is None:
            position = _print_follow_chunk(stdout_path, position)
            break
        if task.id != active_task_id or task.runtime.execution.active_subagent.id != active_subagent_id:
            position = _print_follow_chunk(stdout_path, position)
            break
        if task.runtime.execution.active_subagent.path != active_path:
            position = _print_follow_chunk(stdout_path, position)
            break
        time.sleep(FOLLOW_POLL_SECONDS)
    return 0


def _latest_daemon_log_path(latest_dir: Path | None) -> Path | None:
    """
    Pick the file :func:`show_latest_daemon_log` should tail.

    Prefers the canonical ``*-run.log`` produced by the runner so
    the output matches what the daemon actually emitted; falls
    back to any other file in the directory only when the run log
    is missing (corrupted session, manual deletion). Returns
    ``None`` when nothing is suitable so the caller can print the
    "no logs" message.
    """
    if latest_dir is None or not latest_dir.exists():
        return None
    preferred = sorted(latest_dir.glob("*-run.log"))
    if preferred:
        return preferred[-1]
    candidates = sorted(path for path in latest_dir.iterdir() if path.is_file())
    if candidates:
        return candidates[-1]
    return None


def _tail_text(text: str, lines: int = _DEFAULT_TAIL_LINES) -> str:
    """
    Return the trailing ``lines`` lines of ``text``.

    :func:`show_latest_daemon_log` uses this so the operator
    default does not dump a multi-megabyte daemon log to the
    terminal. Forty lines is enough to cover the wrap-up of a
    typical pool run while still fitting one terminal page.
    """
    text = text.rstrip("\n")
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _print_follow_chunk(stdout_path: Path, position: int) -> int:
    """
    Print the bytes of ``stdout_path`` past ``position``.

    Returns the new file length so the polling loop in
    :func:`follow_active_subagent` only emits the unwritten tail
    on each tick instead of re-printing the whole file. Skips
    silently when the file vanished mid-poll so a delete does not
    crash the follower.
    """
    if not stdout_path.exists():
        return position
    content = stdout_path.read_text(encoding="utf-8")
    if len(content) <= position:
        return position
    print(content[position:], end="")
    return len(content)


def _session_outcome(directory: Path) -> str:
    """
    Recover the pool stop reason for a recorded session.

    Prefers the post-status log (authoritative because it is
    written after the pool finishes) and falls back to scanning
    the run log so older sessions without a post-status file
    still surface a useful label. Returns ``-`` when neither
    source carries the field so the listing column stays
    populated.
    """
    post_status = sorted(directory.glob("*-post-status.log"))
    for path in reversed(post_status):
        for line in read_text_artifact(path).splitlines():
            if line.startswith("pool_stop_reason:"):
                value = line.split(":", 1)[1].strip()
                return value or "-"
    run_logs = sorted(directory.glob("*-run.log"))
    for path in reversed(run_logs):
        for line in reversed(read_text_artifact(path).splitlines()):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip() == "stop_reason":
                return value.strip() or "-"
    return "-"


def _format_session_timestamp(name: str) -> str:
    """
    Convert a ``YYYYmmddTHHMMSSZ`` directory name into an ISO-8601 string.

    Used by :func:`list_daemon_sessions` so the operator sees a
    readable timestamp instead of the compact log-directory form.
    Returns ``-`` for any name not in that exact shape so a stray
    directory under ``logs/run-all`` cannot break the listing.
    """
    try:
        return datetime.strptime(name, "%Y%m%dT%H%M%SZ").isoformat() + "Z"
    except ValueError:
        return "-"


def _latest_subagent_ref(task):
    """
    Pick the subagent that :func:`follow_active_subagent` should attach to.

    Prefers the runtime's currently-active subagent so ``--follow``
    lands on the live process; falls back to the most recent
    persisted ref so a just-finished task is still followable
    (the operator gets the final tail rather than a "nothing to
    follow" message).
    """
    preferred_ids: list[str] = []
    if task.runtime.execution.active_subagent is not None:
        preferred_ids.append(task.runtime.execution.active_subagent.id)
    for subagent_id in preferred_ids:
        for ref in reversed(task.subagents):
            if ref.id == subagent_id:
                return ref
    if task.subagents:
        return task.subagents[-1]
    return None


def _artifact_for_kind(base: Path, kind: str, active: bool) -> Path | None:
    """
    Resolve the on-disk path of a subagent artifact.

    Prefers the live ``.log`` form for active runs (the writer
    streams there) but falls back to the legacy ``.txt`` so
    historic runs remain followable. Currently only ``stdout`` is
    supported; the ``kind`` parameter is here so the caller's
    intent is explicit and a future stderr follower can extend it.
    """
    if kind == "stdout":
        if active:
            live = resolve_artifact_path(base, "stdout.log")
            if live is not None:
                return live
        legacy = resolve_artifact_path(base, "stdout.txt")
        if legacy is not None:
            return legacy
        return resolve_artifact_path(base, "stdout.log")
    raise ValueError(f"Unsupported artifact kind: {kind}")


def _pick_runtime_value(runtime_state: object, *keys: str) -> object:
    """
    Read a field from a live runtime state.

    Live in-memory values (e.g. exit code on a just-finished run)
    win over stale on-disk snapshots in ``list_task_subagents`` so
    the listing reflects the truth at the moment of the call.
    """
    if runtime_state is not None:
        for key in keys:
            value = getattr(runtime_state, key, None)
            if value is not None:
                return value
    return None


def _format_duration(started_at: object, completed_at: object) -> str:
    """
    Render elapsed time between two ISO timestamps as ``Ns``.

    Used by :func:`list_task_subagents` for the duration column.
    Falls back to ``-`` whenever either side is missing or
    unparseable so the table still renders for partial data, and
    a negative duration (clock skew) renders as ``-`` rather than
    a misleading negative number.
    """
    if not started_at or not completed_at:
        return "-"
    if not isinstance(started_at, (str, datetime)) or not isinstance(completed_at, (str, datetime)):
        return "-"
    try:
        start = _coerce_datetime(started_at)
        end = _coerce_datetime(completed_at)
    except ValueError:
        return "-"
    total_seconds = int((end - start).total_seconds())
    if total_seconds >= 0:
        return f"{total_seconds}s"
    return "-"


def resolve_follow_task_for_workspace(workspace: Workspace, task_id: str | None) -> TaskRecord | None:
    """
    Pick the task whose stdout ``--follow`` should attach to.

    Precedence: explicit id > task with an active subagent > most
    recent task that ever had subagents. The fallback chain lets
    the operator run ``task logs --follow`` with no arguments
    mid-run and land on the obviously interesting task without
    typing its id.
    """
    if task_id is not None:
        return workspace.get_task_record(task_id)
    tasks = workspace.list_tasks(strict=False)
    active = next((task for task in tasks if task.runtime.execution.active_subagent is not None), None)
    if active is not None:
        return active
    return next((task for task in tasks if task.subagents), None)


def load_task_with_runtime_for_workspace(workspace: Workspace, task_id: str) -> TaskRecord | None:
    """
    Tolerant task lookup used by ``task logs <id>``.

    Returns ``None`` on missing-runtime/missing-task instead of
    raising so the CLI can print a clean ``task not found``
    rather than a traceback. Trivial wrapper today, but kept as
    the seam where richer runtime hydration can land if the logs
    surface needs it.
    """
    return workspace.get_task_record(task_id)


def _coerce_datetime(value: str | datetime) -> datetime:
    """
    Coerce a runtime or persisted timestamp into a ``datetime``.

    Accepts either an already-parsed ``datetime`` or an ISO
    string (with the SQLite-style trailing ``Z`` suffix) so
    :func:`_format_duration` does not have to care which form it
    received from runtime vs persisted state.
    """
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
