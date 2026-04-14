"""Daemon loop, start/stop, and status helpers."""

from datetime import UTC, datetime
import logging
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

from litehive.attention import list_attention
from litehive.config.loading import load_config
from litehive.config.paths import state_path, workspace_logs_dir
from litehive.config.workspace import ensure_workspace
from litehive.state.backup import create_scheduled_workspace_backup
from litehive.state.persist import set_pool_stop_reason
from litehive.state.locking import runner_status

from .logs import latest_matching, prune_run_all_log_dirs, latest_run_all_log_dir
from .registry import (
    pid_is_alive,
    get_workspace_daemon,
    register_daemon,
    unregister_daemon,
)

logger = logging.getLogger(__name__)

_EXPLICIT_POOL_STOP_REASONS = {
    "attention_required",
    "dirty_git_state",
    "diverged_from_origin",
    "max_tasks_reached",
    "failure_detected",
    "execution_limit_reached",
    "execution_limit_fallbacks_exhausted",
    "stop_condition_reached",
    "human_checkpoint_before_acceptance",
    "human_checkpoint_before_commit",
    "human_checkpoint_reached",
    "continue_or_rollback_required",
    "task_interrupted",
}


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )


def check_origin_divergence(workspace: Path) -> str | None:
    """Return a human-readable reason if local main has diverged from origin/main.

    - Not a git repo, no origin remote, or no main/origin/main ref: returns None (not our concern).
    - Fetch failure: returns None (network issues shouldn't halt the pool).
    - Fast-forward in either direction (including equal): returns None.
    - True divergence (neither is ancestor of the other): returns the reason string.
    """
    if not (workspace / ".git").exists():
        return None
    remotes = _git(workspace, "remote")
    if remotes.returncode != 0 or "origin" not in remotes.stdout.split():
        return None
    branch = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if branch.returncode != 0:
        return None
    local_branch = branch.stdout.strip()
    if local_branch != "main":
        return None
    fetch = _git(workspace, "fetch", "origin", "main")
    if fetch.returncode != 0:
        logger.warning("git fetch origin main failed: %s", fetch.stderr.strip())
        return None
    local_rev = _git(workspace, "rev-parse", "main")
    remote_rev = _git(workspace, "rev-parse", "origin/main")
    if local_rev.returncode != 0 or remote_rev.returncode != 0:
        return None
    local_sha = local_rev.stdout.strip()
    remote_sha = remote_rev.stdout.strip()
    if local_sha == remote_sha:
        return None
    # Either side being an ancestor of the other is a fast-forward — not diverged.
    local_is_ancestor = _git(workspace, "merge-base", "--is-ancestor", local_sha, remote_sha)
    if local_is_ancestor.returncode == 0:
        return None
    remote_is_ancestor = _git(workspace, "merge-base", "--is-ancestor", remote_sha, local_sha)
    if remote_is_ancestor.returncode == 0:
        return None
    return (
        f"local main ({local_sha[:8]}) and origin/main ({remote_sha[:8]}) have diverged. "
        "Manual reconciliation required: run `git fetch origin main`, inspect "
        "`git log --oneline --left-right main...origin/main`, then rebase, reset, or merge "
        "before restarting the pool."
    )


def _write_pool_stop_reason(workspace: Path, reason: str) -> None:
    set_pool_stop_reason(workspace, reason)


def _append_attention_log(workspace: Path, message: str) -> None:
    """Append a timestamped entry to the runtime attention log (gitignored)."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = workspace / ".litehive" / "runtime" / "attention.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp}\t{message}\n")


def _state_snapshot(workspace: Path) -> tuple[dict[str, object], str]:
    sp = state_path(workspace)
    state = yaml.safe_load(sp.read_text(encoding="utf-8")) if sp.exists() else {}
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


def _maybe_run_workspace_backup(
    workspace: Path,
    *,
    now: datetime | None = None,
    stream: TextIO | None,
) -> None:
    backup = create_scheduled_workspace_backup(workspace, now=now)
    if backup is not None:
        _emit(f"backup_created: {backup.timestamp}", stream=stream)


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
    log_base = workspace_logs_dir(workspace) / "run-all"
    log_root = session_dir or (log_base / timestamp)
    log_root.mkdir(parents=True, exist_ok=True)
    prune_run_all_log_dirs(log_base)
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
        consecutive_iteration_failures = 0
        # Daemon-level resilience: an individual iteration crashing (e.g. a
        # corrupt user-global yaml, a stuck lock, a transient subprocess
        # failure) must not kill the whole daemon. Bounded retry with backoff
        # so we don't spin forever on a permanent failure.
        MAX_CONSECUTIVE_FAILURES = 5
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

            try:
                _maybe_run_workspace_backup(workspace, stream=output_stream)
            except Exception as exc:
                logger.exception("scheduled workspace backup failed")
                _append_attention_log(workspace, f"scheduled backup failed: {exc}")
                _emit(f"backup_failed: {exc}", stream=output_stream)

            iteration_failed = False
            iteration_failure_reason: str | None = None

            divergence_reason = check_origin_divergence(workspace)
            if divergence_reason is not None:
                _write_pool_stop_reason(workspace, "diverged_from_origin")
                _append_attention_log(workspace, divergence_reason)
                _emit(
                    "!!! ATTENTION REQUIRED !!! Local main has diverged from origin/main. "
                    "Halting pool: diverged_from_origin",
                    stream=output_stream,
                )
                _emit(divergence_reason, stream=output_stream)
                return 0

            repair_started = time.perf_counter()
            try:
                repair_rc = _run_logged_subprocess(
                    [*command_prefix, "repair", "--workspace", str(workspace)],
                    cwd=workspace,
                    log_path=repair_file,
                    output_stream=None,
                    current_child=current_child,
                )
            except Exception as exc:
                logger.exception("repair subprocess raised")
                _emit(f"repair raised: {exc}", stream=output_stream)
                iteration_failed = True
                iteration_failure_reason = f"repair raised: {exc}"
                repair_rc = -1
            repair_elapsed_ms = int((time.perf_counter() - repair_started) * 1000)
            if repair_rc != 0 and not iteration_failed:
                _emit(f"litehive repair failed (rc={repair_rc}); see {repair_file}",
                      stream=output_stream)
                iteration_failed = True
                iteration_failure_reason = f"repair exited {repair_rc}"
            elif repair_rc == 0:
                repair_status = "completed"
                try:
                    repair_text = repair_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    repair_text = ""
                if "repaired: no" in repair_text:
                    repair_status = "clean"
                elif "repaired: yes" in repair_text:
                    repair_status = "changed"
                _emit(f"repair: {repair_status} ({repair_elapsed_ms}ms)", stream=output_stream)

            if not iteration_failed:
                try:
                    pre_state, pre_snapshot = _state_snapshot(workspace)
                except Exception as exc:
                    logger.exception("pre-status snapshot raised")
                    _emit(f"pre-status raised: {exc}", stream=output_stream)
                    iteration_failed = True
                    iteration_failure_reason = f"pre-status raised: {exc}"
                    pre_state, pre_snapshot = {}, ""
            else:
                pre_state, pre_snapshot = {}, ""

            if iteration_failed:
                consecutive_iteration_failures += 1
                _append_attention_log(
                    workspace,
                    f"daemon iteration {iteration} failed: {iteration_failure_reason}",
                )
                _emit(
                    f"!!! ATTENTION !!! iteration {iteration} failed "
                    f"({consecutive_iteration_failures}/{MAX_CONSECUTIVE_FAILURES}): "
                    f"{iteration_failure_reason}",
                    stream=output_stream,
                )
                if consecutive_iteration_failures >= MAX_CONSECUTIVE_FAILURES:
                    _emit(
                        f"Too many consecutive iteration failures "
                        f"({consecutive_iteration_failures}). Halting pool.",
                        stream=output_stream,
                    )
                    _write_pool_stop_reason(workspace, "daemon_iteration_failures")
                    return 0
                # Short backoff before the next attempt
                time.sleep(min(5 * consecutive_iteration_failures, 30))
                continue

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
            if load_config(workspace).pool_stop_on_attention:
                unresolved_attention = list_attention(workspace)
                if unresolved_attention:
                    _write_pool_stop_reason(workspace, "attention_required")
                    _emit(
                        f"Pool stopped: attention_required ({len(unresolved_attention)} unresolved item(s))",
                        stream=output_stream,
                    )
                    return 0

            try:
                run_rc = _run_logged_subprocess(
                    [*command_prefix, "run", "--workspace", str(workspace)],
                    cwd=workspace,
                    log_path=run_file,
                    output_stream=output_stream,
                    current_child=current_child,
                )
            except Exception as exc:
                logger.exception("run subprocess raised")
                _emit(f"run raised: {exc}", stream=output_stream)
                iteration_failed = True
                iteration_failure_reason = f"run raised: {exc}"
                run_rc = -1
            if run_rc != 0 and not iteration_failed:
                _emit(f"litehive run failed (rc={run_rc}); see {run_file}",
                      stream=output_stream)
                iteration_failed = True
                iteration_failure_reason = f"run exited {run_rc}"

            if iteration_failed:
                consecutive_iteration_failures += 1
                _append_attention_log(
                    workspace,
                    f"daemon iteration {iteration} failed: {iteration_failure_reason}",
                )
                _emit(
                    f"!!! ATTENTION !!! iteration {iteration} failed "
                    f"({consecutive_iteration_failures}/{MAX_CONSECUTIVE_FAILURES}): "
                    f"{iteration_failure_reason}",
                    stream=output_stream,
                )
                if consecutive_iteration_failures >= MAX_CONSECUTIVE_FAILURES:
                    _emit(
                        f"Too many consecutive iteration failures "
                        f"({consecutive_iteration_failures}). Halting pool.",
                        stream=output_stream,
                    )
                    _write_pool_stop_reason(workspace, "daemon_iteration_failures")
                    return 0
                time.sleep(min(5 * consecutive_iteration_failures, 30))
                continue

            # Reset the failure counter after a successful iteration
            consecutive_iteration_failures = 0

            try:
                post_state, post_snapshot = _state_snapshot(workspace)
            except Exception as exc:
                logger.exception("post-status snapshot raised")
                _emit(f"post-status raised: {exc}", stream=output_stream)
                continue
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
    project_root = Path(__file__).resolve().parents[2]
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
        if not pid_is_alive(pid):
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
    latest_post = latest_matching(latest_dir, "*-post-status.log")
    lines.append(f"latest_post_status: {latest_post if latest_post is not None else '-'}")
    latest_run = latest_matching(latest_dir, "*-run.log")
    lines.append(f"latest_run_log: {latest_run if latest_run is not None else '-'}")
    return lines
