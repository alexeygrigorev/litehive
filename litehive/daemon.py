"""Daemon lifecycle helpers for Litehive pool execution."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
from typing import TextIO

import yaml

from litehive.config import ensure_workspace
from litehive.models import utcnow
from litehive.tasks import runner_status

_EXPLICIT_POOL_STOP_REASONS = {
    "dirty_git_state",
    "max_tasks_reached",
    "failure_detected",
    "execution_limit_reached",
    "execution_limit_fallbacks_exhausted",
    "quota_threshold_reached",
    "budget_threshold_reached",
    "stop_condition_reached",
    "pool_usage_cap_reached",
    "pool_cost_cap_reached",
    "human_checkpoint_before_acceptance",
    "human_checkpoint_before_commit",
    "human_checkpoint_reached",
    "continue_or_rollback_required",
    "task_interrupted",
}
_RUN_ALL_SESSION_RETENTION = 8


def daemon_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base).expanduser() / "litehive"
    return Path.home() / ".config" / "litehive"


def daemon_registry_path() -> Path:
    return daemon_config_dir() / "daemons.yaml"


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _empty_registry() -> dict[str, object]:
    return {"daemons": {}}


@contextmanager
def _locked_registry() -> dict[str, object]:
    path = daemon_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read()
        data = yaml.safe_load(raw) if raw.strip() else _empty_registry()
        if not isinstance(data, dict):
            data = _empty_registry()
        daemons = data.get("daemons")
        if not isinstance(daemons, dict):
            data["daemons"] = {}
        _prune_registry_in_place(data)
        yield data
        handle.seek(0)
        handle.truncate()
        yaml.safe_dump(data, handle, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prune_registry_in_place(data: dict[str, object]) -> None:
    daemons = data.setdefault("daemons", {})
    if not isinstance(daemons, dict):
        data["daemons"] = {}
        return
    stale: list[str] = []
    for workspace, payload in daemons.items():
        if not isinstance(payload, dict):
            stale.append(workspace)
            continue
        pid = payload.get("pid")
        if not isinstance(pid, int) or not _pid_is_alive(pid):
            stale.append(workspace)
    for workspace in stale:
        daemons.pop(workspace, None)


def list_daemon_instances() -> list[dict[str, object]]:
    with _locked_registry() as data:
        daemons = data.get("daemons", {})
        if not isinstance(daemons, dict):
            return []
        return [dict(payload) for _, payload in sorted(daemons.items()) if isinstance(payload, dict)]


def get_workspace_daemon(workspace: Path) -> dict[str, object] | None:
    workspace = str(workspace.resolve())
    with _locked_registry() as data:
        daemons = data.get("daemons", {})
        if not isinstance(daemons, dict):
            return None
        payload = daemons.get(workspace)
        return dict(payload) if isinstance(payload, dict) else None


def register_daemon(workspace: Path, *, pid: int, log_dir: Path) -> None:
    workspace = workspace.resolve()
    workspace_key = str(workspace)
    with _locked_registry() as data:
        daemons = data.setdefault("daemons", {})
        assert isinstance(daemons, dict)
        existing = daemons.get(workspace_key)
        if isinstance(existing, dict):
            existing_pid = existing.get("pid")
            if isinstance(existing_pid, int) and existing_pid != pid and _pid_is_alive(existing_pid):
                raise RuntimeError(f"daemon already running for {workspace_key}: pid={existing_pid}")
        daemons[workspace_key] = {
            "workspace": workspace_key,
            "pid": pid,
            "started_at": utcnow(),
            "log_dir": str(log_dir),
        }


def unregister_daemon(workspace: Path, *, pid: int | None = None) -> None:
    workspace_key = str(workspace.resolve())
    with _locked_registry() as data:
        daemons = data.setdefault("daemons", {})
        assert isinstance(daemons, dict)
        existing = daemons.get(workspace_key)
        if not isinstance(existing, dict):
            return
        if pid is not None and existing.get("pid") != pid:
            return
        daemons.pop(workspace_key, None)


def latest_run_all_log_dir(workspace: Path) -> Path | None:
    log_base = workspace.resolve() / ".litehive" / "logs" / "run-all"
    if not log_base.exists():
        return None
    candidates = sorted(path for path in log_base.iterdir() if path.is_dir())
    return candidates[-1] if candidates else None


def _prune_run_all_log_dirs(log_base: Path, *, keep: int = _RUN_ALL_SESSION_RETENTION) -> None:
    if not log_base.exists():
        return
    directories = sorted(path for path in log_base.iterdir() if path.is_dir())
    for directory in directories[:-keep]:
        shutil.rmtree(directory, ignore_errors=True)


def _latest_matching(log_dir: Path | None, pattern: str) -> Path | None:
    if log_dir is None or not log_dir.exists():
        return None
    matches = sorted(log_dir.glob(pattern))
    return matches[-1] if matches else None


def _state_snapshot(workspace: Path) -> tuple[dict[str, object], str]:
    state_path = workspace / ".litehive" / "state.yaml"
    state = yaml.safe_load(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not isinstance(state, dict):
        state = {}
    active_task_id = state.get("active_task_id")
    queue = state.get("queue", []) or []
    stop_reason = state.get("pool_stop_reason")
    lines = [
        f"active_task_id: {active_task_id if active_task_id is not None else 'None'}",
        f"queued_tasks: {len(queue)}",
        f"pool_stop_reason: {stop_reason if stop_reason is not None else 'None'}",
    ]
    if queue:
        lines.append(f"queue_head: {queue[0]}")
    return state, "\n".join(lines) + "\n"


def _is_explicit_pool_stop_reason(reason: str | None) -> bool:
    return bool(reason and reason in _EXPLICIT_POOL_STOP_REASONS)


def _default_command_prefix() -> list[str]:
    override = os.environ.get("LITEHIVE_DAEMON_EXECUTABLE")
    if override:
        return shlex.split(override)
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "litehive"]
    litehive_bin = shutil.which("litehive")
    if litehive_bin:
        return [litehive_bin]
    return [sys.executable, "-m", "litehive.main"]


def _emit(message: str, *, stream: TextIO | None) -> None:
    if stream is None:
        return
    stream.write(message)
    if not message.endswith("\n"):
        stream.write("\n")
    stream.flush()


def _run_logged_subprocess(
    command: list[str],
    *,
    cwd: Path,
    log_path: Path,
    output_stream: TextIO | None,
    current_child: dict[str, subprocess.Popen[str] | None],
) -> int:
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        current_child["process"] = process
        assert process.stdout is not None
        for line in process.stdout:
            log_handle.write(line)
            log_handle.flush()
            if output_stream is not None:
                output_stream.write(line)
                output_stream.flush()
        return_code = process.wait()
        current_child["process"] = None
        return return_code


def run_daemon_loop(
    workspace: Path,
    *,
    output_stream: TextIO | None = None,
    session_dir: Path | None = None,
) -> int:
    workspace = workspace.resolve()
    ensure_workspace(workspace)
    command_prefix = _default_command_prefix()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_base = workspace / ".litehive" / "logs" / "run-all"
    log_root = session_dir or (log_base / timestamp)
    log_root.mkdir(parents=True, exist_ok=True)
    _prune_run_all_log_dirs(log_base)
    register_daemon(workspace, pid=os.getpid(), log_dir=log_root)

    stop_requested = False
    current_child: dict[str, subprocess.Popen[str] | None] = {"process": None}

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        child = current_child["process"]
        if child is not None and child.poll() is None:
            child.terminate()

    previous_term = signal.signal(signal.SIGTERM, _handle_signal)
    previous_int = signal.signal(signal.SIGINT, _handle_signal)
    try:
        _emit(f"workspace: {workspace}", stream=output_stream)
        _emit(f"logs: {log_root}", stream=output_stream)
        iteration = 0
        while True:
            if stop_requested:
                _emit("Daemon stop requested. Stopping.", stream=output_stream)
                return 0

            iteration += 1
            prefix = f"{iteration:04d}"
            repair_file = log_root / f"{prefix}-repair.log"
            pre_status_file = log_root / f"{prefix}-pre-status.log"
            run_file = log_root / f"{prefix}-run.log"
            post_status_file = log_root / f"{prefix}-post-status.log"

            _emit("", stream=output_stream)
            _emit(f"== iteration {iteration} ==", stream=output_stream)

            repair_rc = _run_logged_subprocess(
                [*command_prefix, "repair", "--workspace", str(workspace)],
                cwd=workspace,
                log_path=repair_file,
                output_stream=None,
                current_child=current_child,
            )
            if repair_rc != 0:
                _emit(f"litehive repair failed; see {repair_file}", stream=output_stream)
                return 1

            pre_state, pre_snapshot = _state_snapshot(workspace)
            pre_status_file.write_text(pre_snapshot, encoding="utf-8")
            _emit(pre_snapshot, stream=output_stream)

            active_task_id = pre_state.get("active_task_id")
            queue = pre_state.get("queue", []) or []
            stop_reason_before = pre_state.get("pool_stop_reason")

            if active_task_id is None and not queue:
                _emit("No active or queued tasks remain. Stopping.", stream=output_stream)
                return 0
            if stop_reason_before == "blocked_tasks_remaining":
                _emit("Blocked tasks remain and nothing is runnable. Stopping.", stream=output_stream)
                return 0
            if _is_explicit_pool_stop_reason(
                str(stop_reason_before) if stop_reason_before is not None else None
            ):
                _emit(f"Pool already stopped: {stop_reason_before}", stream=output_stream)
                return 0

            run_rc = _run_logged_subprocess(
                [*command_prefix, "run", "--workspace", str(workspace)],
                cwd=workspace,
                log_path=run_file,
                output_stream=output_stream,
                current_child=current_child,
            )
            if run_rc != 0:
                _emit(f"litehive run failed; see {run_file}", stream=output_stream)
                return 1

            post_state, post_snapshot = _state_snapshot(workspace)
            post_status_file.write_text(post_snapshot, encoding="utf-8")
            _emit(post_snapshot, stream=output_stream)

            active_after = post_state.get("active_task_id")
            queue_after = post_state.get("queue", []) or []
            stop_reason = post_state.get("pool_stop_reason")

            if active_after is None and not queue_after:
                _emit("No active or queued tasks remain. Stopping.", stream=output_stream)
                return 0
            if stop_reason == "blocked_tasks_remaining":
                _emit("Blocked tasks remain and nothing is runnable. Stopping.", stream=output_stream)
                return 0
            if _is_explicit_pool_stop_reason(str(stop_reason) if stop_reason is not None else None):
                _emit(f"Pool stopped: {stop_reason}", stream=output_stream)
                return 0
            if stop_reason == "task_requeued":
                continue
            if stop_reason not in {None, "None", "queue_exhausted"}:
                _emit(
                    f"Stopping after litehive reported stop_reason: {stop_reason}",
                    stream=output_stream,
                )
                return 0
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        unregister_daemon(workspace, pid=os.getpid())


def start_background_daemon(workspace: Path) -> int:
    workspace = workspace.resolve()
    existing = get_workspace_daemon(workspace)
    if existing is not None:
        pid = existing.get("pid")
        raise RuntimeError(f"daemon already running for {workspace}: pid={pid}")
    project_root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "litehive.main",
            "daemon",
            "worker",
            "--workspace",
            str(workspace),
        ],
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("daemon failed to start")
        entry = get_workspace_daemon(workspace)
        if entry is not None and entry.get("pid") == process.pid:
            return process.pid
        time.sleep(0.1)
    raise RuntimeError("daemon did not register before timeout")


def stop_workspace_daemon(workspace: Path) -> dict[str, object] | None:
    workspace = workspace.resolve()
    entry = get_workspace_daemon(workspace)
    if entry is None:
        return None
    pid = entry.get("pid")
    if not isinstance(pid, int):
        unregister_daemon(workspace)
        return None
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            unregister_daemon(workspace, pid=pid)
            return entry
        time.sleep(0.1)
    return entry


def daemon_status_lines(workspace: Path) -> list[str]:
    workspace = workspace.resolve()
    entry = get_workspace_daemon(workspace)
    lines = [f"workspace: {workspace}"]
    if entry is None:
        lines.append("daemon_status: stopped")
    else:
        lines.append("daemon_status: running")
        lines.append(f"pid: {entry.get('pid')}")
        lines.append(f"started_at: {entry.get('started_at')}")
        lines.append(f"log_dir: {entry.get('log_dir')}")
    runner = runner_status(workspace)
    lines.append(
        "runner_status: "
        f"{runner.status} pid={runner.pid or '-'} "
        f"started_at={runner.started_at or '-'} "
        f"heartbeat_at={runner.heartbeat_at or '-'} "
        f"active_task_id={runner.active_task_id or '-'}"
    )
    latest_dir = latest_run_all_log_dir(workspace)
    lines.append(f"latest_run_all_dir: {latest_dir if latest_dir is not None else '-'}")
    latest_post = _latest_matching(latest_dir, "*-post-status.log")
    lines.append(f"latest_post_status: {latest_post if latest_post is not None else '-'}")
    latest_run = _latest_matching(latest_dir, "*-run.log")
    lines.append(f"latest_run_log: {latest_run if latest_run is not None else '-'}")
    return lines
