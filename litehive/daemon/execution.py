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
import threading
import time
from typing import TextIO

from litehive.config.paths import workspace_path
from litehive.config.workspace import ensure_workspace
from litehive.attention import append_attention_log
from litehive.db.schema import apply_pending_migrations
from litehive.workspace import Workspace
from litehive.git.ops import fetch, is_ancestor, list_remote_names, rev_parse_verify
from litehive.observability.status import (
    collect_task_pipeline_status,
    render_runner_status_line,
    render_task_pipeline_status_lines,
)
from litehive.observability.venv_health import daemon_broken_venv_message, probe_broken_venv_executables
from litehive.state.backup import create_scheduled_workspace_backup
from litehive.state.persist import load_state, set_pool_stop_reason
from litehive.state.locking import runner_status

from .logs import latest_matching, prune_run_all_log_dirs, latest_run_all_log_dir
from .registry import (
    daemon_metadata,
    pid_is_alive,
    get_workspace_daemon,
    register_daemon,
    touch_daemon,
    unregister_daemon,
)

logger = logging.getLogger(__name__)

_DAEMON_HEARTBEAT_INTERVAL_SECONDS = 1.0
_DAEMON_HEALTH_TIMEOUT_SECONDS = 10.0
DAEMON_STOP_GRACE_PERIOD_SECONDS = 5.0
DAEMON_FORCE_KILL_TIMEOUT_SECONDS = 4.0
DAEMON_EXIT_POLL_INTERVAL_SECONDS = 0.1

_CONTINUE_STOP_REASONS = {None, "None", "queue_exhausted", "task_requeued"}


def check_origin_divergence(workspace: Path) -> str | None:
    """Return a human-readable reason if local main has diverged from origin/main.

    - Not a git repo, no origin remote, or no main/origin/main ref: returns None (not our concern).
    - Fetch failure: returns None (network issues shouldn't halt the pool).
    - Fast-forward in either direction (including equal): returns None.
    - True divergence (neither is ancestor of the other): returns the reason string.

    The daemon compares the refs directly instead of relying on the current
    checkout, because pool safety depends on `main` vs `origin/main` even when
    `HEAD` is elsewhere.
    """
    if not (workspace / ".git").exists():
        return None
    if "origin" not in list_remote_names(workspace):
        return None
    ok, stderr = fetch(workspace, "origin", "main")
    if not ok:
        logger.warning("git fetch origin main failed: %s", stderr)
        return None
    local_sha = rev_parse_verify(workspace, "main")
    remote_sha = rev_parse_verify(workspace, "origin/main")
    if local_sha is None or remote_sha is None:
        return None
    if local_sha == remote_sha:
        return None
    # Either side being an ancestor of the other is a fast-forward — not diverged.
    if is_ancestor(workspace, local_sha, remote_sha):
        return None
    if is_ancestor(workspace, remote_sha, local_sha):
        return None
    return (
        f"local main ({local_sha[:8]}) and origin/main ({remote_sha[:8]}) have diverged. "
        "Manual reconciliation required: run `git fetch origin main`, inspect "
        "`git log --oneline --left-right main...origin/main`, then rebase, reset, or merge "
        "before restarting the pool."
    )


def _halt_for_origin_divergence(
    workspace: Path,
    output_stream: TextIO | None,
) -> bool:
    """Stop the pool and raise an attention flag when local main has diverged from origin.

    Returns True when the daemon loop should exit; merging or rebasing
    a diverged main is a human decision, not a daemon decision.
    """
    divergence_reason = check_origin_divergence(workspace)
    if divergence_reason is None:
        return False
    _write_pool_stop_reason(workspace, "diverged_from_origin")
    _append_attention_log(workspace, divergence_reason)
    _emit(
        "!!! ATTENTION REQUIRED !!! Local main has diverged from origin/main. Halting pool: diverged_from_origin",
        stream=output_stream,
    )
    _emit(divergence_reason, stream=output_stream)
    return True


def _write_pool_stop_reason(workspace: Path, reason: str) -> None:
    """Persist a pool stop reason via the canonical state writer; daemon-internal alias kept so divergence/halt branches read uniformly."""
    set_pool_stop_reason(workspace, reason)


def sleep_with_stop(seconds: float, stop_requested_fn) -> None:
    """Sleep up to ``seconds``, returning early if a stop is requested."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or stop_requested_fn():
            return
        time.sleep(min(remaining, 1.0))


def _append_attention_log(workspace: Path, message: str) -> None:
    """Persist a daemon-side attention entry through the canonical store.

    Daemon used to maintain its own file-based duplicate of the
    attention log; the litehive-wide rule is "everything in SQLite",
    so this thin wrapper exists only to keep the import surface
    daemon-internal while delegating to ``litehive.attention``.
    """
    append_attention_log(Workspace.from_path(workspace), message)


def _daemon_status_snapshot(workspace: Path) -> tuple[dict[str, object], str]:
    """Capture pool state plus a renderable status block in one read-only pass.

    The daemon loop logs the snapshot before and after each ``litehive run``
    invocation; using ``read_only=True`` keeps the snapshot from racing the
    runner's own writes.
    """
    status = collect_task_pipeline_status(workspace, read_only=True)
    state = status.state.model_dump(mode="python")
    lines = render_task_pipeline_status_lines(status, workspace=workspace, mode="fast")
    return state, "\n".join(lines) + "\n"


def default_command_prefix() -> list[str]:
    """Pick the argv prefix the daemon uses to launch a child ``litehive run``.

    Honors ``LITEHIVE_DAEMON_EXECUTABLE`` for tests/operators that need a
    pinned executable, then prefers ``uv run`` for development workspaces,
    falls back to an installed ``litehive`` binary, and finally re-enters
    ``python -m litehive.main`` so the daemon still works when neither
    launcher is on PATH.
    """
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


def _emit(message: str, stream: TextIO | None) -> None:
    """Write a daemon-loop status line that survives interleaving with subprocess output.

    The trailing newline + flush is load-bearing: log readers tail the
    file while ``litehive run`` writes to it, so partial lines confuse
    readers. ``stream is None`` keeps non-interactive daemon mode silent
    without forcing every call site to branch.
    """
    if stream is None:
        return
    stream.write(message)
    if not message.endswith("\n"):
        stream.write("\n")
    stream.flush()


def _heartbeat_age_seconds(heartbeat_at: object) -> float | None:
    """Compute how stale a recorded daemon heartbeat is, tolerating bad input.

    Returns None for missing/non-string/unparseable timestamps so the
    healthcheck treats them as "no recent heartbeat" rather than crashing
    the start-background path; naive timestamps are interpreted as UTC
    because old daemon registrations didn't store tzinfo.
    """
    if not isinstance(heartbeat_at, str) or not heartbeat_at:
        return None
    try:
        timestamp = datetime.fromisoformat(heartbeat_at)
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - timestamp.astimezone(UTC)).total_seconds())


def _daemon_healthcheck_failed(entry: dict[str, object]) -> bool:
    """Decide whether a registered daemon is wedged and should be SIGKILLed before restart.

    A daemon row that says ``status=running`` but has not heartbeat in
    ``_DAEMON_HEALTH_TIMEOUT_SECONDS`` is treated as dead so the
    start-background path can reclaim the workspace instead of refusing
    to start.
    """
    heartbeat_age = _heartbeat_age_seconds(entry.get("heartbeat_at"))
    return heartbeat_age is None or heartbeat_age > _DAEMON_HEALTH_TIMEOUT_SECONDS


def _wait_for_pid_exit(pid: int, timeout_seconds: float) -> bool:
    """Block until a foreign pid is gone or the deadline passes, polling cheaply.

    Used by the SIGTERM/SIGKILL stop sequence: we own the registration
    row, not the process handle, so ``Popen.wait`` is unavailable and
    we have to poll ``pid_is_alive`` instead.
    """
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        if not pid_is_alive(pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, DAEMON_EXIT_POLL_INTERVAL_SECONDS))


def _clear_recorded_daemon(workspace: Path, pid: int) -> None:
    """Drop the daemon registration row after we have confirmed the process is gone.

    Pinning the unregister to ``pid=`` prevents stop sequences from
    accidentally clearing a newer daemon that registered for the same
    workspace while we were waiting for the previous one to die.
    """
    unregister_daemon(workspace, pid=pid)


def _force_kill_recorded_daemon(workspace: Path, pid: int) -> None:
    """SIGKILL a daemon that ignored SIGTERM, then clear its registration.

    Reached either as the second step of the graceful stop sequence or
    directly from ``start_background_daemon`` when an existing record
    fails its heartbeat check. Raises if the process refuses to die so
    the caller doesn't think the workspace is free.
    """
    if not pid_is_alive(pid):
        _clear_recorded_daemon(workspace, pid=pid)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        _clear_recorded_daemon(workspace, pid=pid)
        return
    except PermissionError as exc:
        raise RuntimeError(f"failed to send SIGKILL to daemon pid={pid}: {exc}") from exc
    if not _wait_for_pid_exit(pid, timeout_seconds=DAEMON_FORCE_KILL_TIMEOUT_SECONDS):
        raise RuntimeError(f"daemon pid={pid} did not exit after SIGKILL")
    _clear_recorded_daemon(workspace, pid=pid)


def _terminate_recorded_daemon(workspace: Path, pid: int) -> None:
    """Run the graceful-then-forceful stop sequence against a registered daemon.

    SIGTERM first so the daemon can flush its heartbeat thread and
    unregister cleanly; if that doesn't land within the grace period we
    escalate to SIGKILL via ``_force_kill_recorded_daemon``. Called by
    ``stop_workspace_daemon`` when the operator (or a CLI command)
    asks the workspace's daemon to stop.
    """
    if not pid_is_alive(pid):
        _clear_recorded_daemon(workspace, pid=pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_recorded_daemon(workspace, pid=pid)
        return
    except PermissionError as exc:
        raise RuntimeError(f"failed to send SIGTERM to daemon pid={pid}: {exc}") from exc
    if _wait_for_pid_exit(pid, timeout_seconds=DAEMON_STOP_GRACE_PERIOD_SECONDS):
        _clear_recorded_daemon(workspace, pid=pid)
        return
    _force_kill_recorded_daemon(workspace, pid=pid)


def _terminate_child_process(process: subprocess.Popen[str]) -> None:
    """Forward stop signals from the daemon to the entire ``litehive run`` process group.

    The child runs in its own session (``start_new_session=True``) so it
    can spawn agents that themselves spawn subprocesses; killing only
    the immediate child would orphan that subtree. ``killpg`` covers the
    whole group, with ``process.terminate`` as a fallback when we can't
    address the group (e.g., on platforms without ``killpg``).
    """
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()


def _runner_is_live(status) -> bool:
    """True when another ``litehive run`` is still active for the workspace (running or late but not declared stale); the daemon loop idles instead of spawning a duplicate runner."""
    return getattr(status, "status", None) in {"running", "late"}


def _has_work(state: dict[str, object]) -> bool:
    """True when the pool still has something to do — an active task or a queued task; the loop exits cleanly when this returns False so an empty queue doesn't keep the daemon busy."""
    return state.get("active_task_id") is not None or bool(state.get("queue", []) or [])


def _should_continue_for_stop_reason(reason: object) -> bool:
    """Distinguish "the previous run paused for a transient reason" from "the pool is halted".

    The daemon loop should keep iterating after ``queue_exhausted`` or
    ``task_requeued`` (those clear themselves once new work arrives) but
    must stop on every other reason — operator halts, divergence, etc.
    """
    if reason is None:
        return True
    return str(reason) in _CONTINUE_STOP_REASONS


def _emit_runner_wait(status, stream: TextIO | None) -> None:
    """Log enough about the live runner that an operator can tell why the daemon is idling.

    The daemon loop hits this branch when another ``litehive run`` is
    still active for the workspace; surfacing pid + active task + last
    heartbeat lets the operator decide whether to wait or intervene
    instead of seeing a silent loop.
    """
    if getattr(status, "pid", None) is None:
        pid = "-"
    else:
        pid = str(status.pid)
    task_id = getattr(status, "active_task_id", None) or "-"
    heartbeat = getattr(status, "heartbeat_at", None) or "-"
    state = getattr(status, "status", None) or "running"
    _emit(
        f"runner already active: status={state} pid={pid} active_task_id={task_id} heartbeat_at={heartbeat}",
        stream=stream,
    )


def ensure_workspace_venvs_ready(
    workspace: Path,
) -> None:
    """Refuse to start the daemon when the workspace has a broken Python venv.

    A broken venv would surface as cryptic agent-side import failures;
    catching it at daemon-start time produces one clear error per
    workspace instead of a wave of failed tasks.
    """
    findings = probe_broken_venv_executables(workspace)
    if findings:
        raise RuntimeError(daemon_broken_venv_message(workspace, findings))


def maybe_run_workspace_backup(
    workspace: Path,
    *,
    now: datetime | None = None,
    stream: TextIO | None,
) -> None:
    """Trigger the scheduled workspace backup once per daemon iteration if it is due.

    The backup policy lives in ``state.backup``; this hook is the only
    place the daemon loop calls into it, so the cadence of "at most one
    backup per loop turn" is enforced here rather than inside the
    backup module.
    """
    backup = create_scheduled_workspace_backup(workspace, now=now)
    if backup is None:
        return
    _emit(f"backup_created: {backup.timestamp}", stream=stream)


def run_logged_subprocess(
    command: list[str],
    cwd: Path,
    log_path: Path,
    output_stream: TextIO | None,
    current_child: dict[str, subprocess.Popen[str] | None],
) -> int:
    """Spawn a child ``litehive run`` and tee its output to a per-iteration log file.

    The child is started in its own process group so the daemon's
    SIGTERM handler can kill the whole subtree, and the live ``Popen``
    is published to ``current_child`` for that handler to reach. Each
    line is flushed twice (log + optional operator stream) so a tail
    of the iteration log stays current while the run is in progress.
    """
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
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
    output_stream: TextIO | None = None,
    session_dir: Path | None = None,
) -> int:
    """Drive a workspace's pool by repeatedly spawning ``litehive run`` until work runs out.

    This is the daemon worker body — invoked by ``daemon worker`` (the
    detached child of ``start_background_daemon``) and by foreground
    ``daemon run`` for development. Owns the heartbeat thread,
    iteration logs, signal forwarding, and the stop-reason policy that
    decides when the pool is genuinely done versus paused.
    """
    workspace = workspace.resolve()
    ensure_workspace(workspace)
    apply_pending_migrations(workspace)
    command_prefix = default_command_prefix()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_base = workspace_path(workspace, "logs", "run-all")
    log_root = session_dir or (log_base / timestamp)
    log_root.mkdir(parents=True, exist_ok=True)
    prune_run_all_log_dirs(log_base)
    register_daemon(workspace, pid=os.getpid(), log_dir=log_root)
    stop_requested = False
    current_child: dict[str, subprocess.Popen[str] | None] = {"process": None}
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_daemon_heartbeat_loop,
        name="litehive-daemon-heartbeat",
        args=(workspace, os.getpid(), heartbeat_stop),
        daemon=True,
    )
    heartbeat_thread.start()

    def _handle_signal(signum: int, _frame: object) -> None:
        """SIGTERM/SIGINT handler installed by ``run_daemon_loop``; flips the stop flag and forwards the signal to the live ``litehive run`` child so a Ctrl-C reaches the whole subtree."""
        nonlocal stop_requested
        del signum
        stop_requested = True
        child = current_child["process"]
        if child is not None:
            _terminate_child_process(child)

    previous_term = signal.signal(signal.SIGTERM, _handle_signal)
    previous_int = signal.signal(signal.SIGINT, _handle_signal)
    try:
        _emit(f"workspace: {workspace}", stream=output_stream)
        _emit(f"logs: {log_root}", stream=output_stream)
        iteration = 0
        while True:
            if stop_requested:
                _emit("Runner stop requested. Stopping.", stream=output_stream)
                return 0

            iteration += 1
            prefix = f"{iteration:04d}"
            run_file = log_root / f"{prefix}-run.log"

            _emit("", stream=output_stream)
            _emit(f"== iteration {iteration} ==", stream=output_stream)

            live_runner = runner_status(workspace)
            if _runner_is_live(live_runner):
                _emit_runner_wait(live_runner, stream=output_stream)
                sleep_with_stop(1.0, stop_requested_fn=lambda: stop_requested)
                continue

            if _halt_for_origin_divergence(workspace, output_stream=output_stream):
                return 0

            try:
                maybe_run_workspace_backup(workspace, stream=output_stream)
            except (OSError, RuntimeError) as exc:
                logger.exception("scheduled workspace backup failed")
                _append_attention_log(workspace, f"scheduled backup failed: {exc}")
                _emit(f"backup_failed: {exc}", stream=output_stream)

            try:
                pre_state, pre_snapshot = _daemon_status_snapshot(workspace)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.exception("status snapshot raised")
                _emit(f"status raised: {exc}", stream=output_stream)
                return 1
            _emit(pre_snapshot, stream=output_stream)

            if not _has_work(pre_state):
                _emit("No active or queued tasks remain. Stopping.", stream=output_stream)
                return 0
            stop_reason_before = pre_state.get("pool_stop_reason")
            if not _should_continue_for_stop_reason(stop_reason_before):
                _emit(f"Runner stopped: {stop_reason_before}", stream=output_stream)
                return 0

            try:
                run_rc = run_logged_subprocess(
                    [*command_prefix, "run", "--workspace", str(workspace)],
                    cwd=workspace,
                    log_path=run_file,
                    output_stream=output_stream,
                    current_child=current_child,
                )
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                logger.exception("run subprocess raised")
                _emit(f"run raised: {exc}", stream=output_stream)
                return 1
            if run_rc != 0:
                _emit(f"litehive run failed (rc={run_rc}); see {run_file}", stream=output_stream)
                return run_rc

            try:
                post_state, post_snapshot = _daemon_status_snapshot(workspace)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.exception("post-status snapshot raised")
                _emit(f"post-status raised: {exc}", stream=output_stream)
                return 1
            _emit(post_snapshot, stream=output_stream)

            stop_reason = post_state.get("pool_stop_reason")
            if not _has_work(post_state):
                _emit("No active or queued tasks remain. Stopping.", stream=output_stream)
                return 0
            if not _should_continue_for_stop_reason(stop_reason):
                _emit(f"Runner stopped: {stop_reason}", stream=output_stream)
                return 0
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=max(_DAEMON_HEARTBEAT_INTERVAL_SECONDS, 0.1) * 2)
        unregister_daemon(workspace, pid=os.getpid())


def _daemon_heartbeat_loop(workspace: Path, pid: int, stop_event: threading.Event) -> None:
    """Refresh the daemon registration's heartbeat row while the worker is alive.

    Runs in a background thread so a long ``litehive run`` iteration
    doesn't make the daemon look dead to ``_daemon_healthcheck_failed``;
    exits as soon as the run-loop sets ``stop_event`` during shutdown.
    """
    while not stop_event.wait(_DAEMON_HEARTBEAT_INTERVAL_SECONDS):
        touch_daemon(workspace, pid=pid)


def start_background_daemon(workspace: Path) -> int:
    """Detach a daemon worker for ``workspace`` and return once it has registered.

    Reclaims a wedged registration (running but missed its heartbeat)
    so an operator who Ctrl-C'd a previous daemon can re-run
    ``litehive daemon start`` without manual cleanup, and waits for the
    child to write its row before returning so callers can rely on the
    registry being current.
    """
    workspace = workspace.resolve()
    existing = daemon_metadata(workspace)
    if existing is not None and existing.get("status") == "running":
        pid = existing.get("pid")
        if isinstance(pid, int) and _daemon_healthcheck_failed(existing):
            _force_kill_recorded_daemon(workspace, pid=pid)
        else:
            raise RuntimeError(f"daemon already running for {workspace}: pid={pid}")
    if existing is not None and existing.get("status") == "stale":
        unregister_daemon(workspace)
    ensure_workspace_venvs_ready(workspace)
    project_root = Path(__file__).resolve().parents[2]
    child_env = os.environ.copy()
    for key in ("LITEHIVE_AGENT_ROLE", "LITEHIVE_STAGE", "LITEHIVE_TASK_ID"):
        child_env.pop(key, None)
    child_env["LITEHIVE_WORKSPACE_ROOT"] = str(workspace)
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
        env=child_env,
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
    """Stop the daemon registered for ``workspace``, returning the prior registration entry.

    Called by ``litehive daemon stop`` and the ``litehive stop`` shortcut.
    Returns ``None`` when there was no daemon to stop so the CLI can
    distinguish "nothing to do" from "stopped a running daemon" without
    a second registry round-trip; also drops stale registrations as a
    side effect.
    """
    workspace = workspace.resolve()
    entry = daemon_metadata(workspace)
    if entry is None:
        return None
    if entry.get("status") != "running":
        unregister_daemon(workspace)
        return None
    pid = entry.get("pid")
    if not isinstance(pid, int):
        unregister_daemon(workspace)
        return None
    _terminate_recorded_daemon(workspace, pid=pid)
    return entry


def daemon_status_lines(workspace: Path) -> list[str]:
    """Render the daemon-side block of ``litehive status`` / ``litehive daemon status``.

    Combines the daemon registration row with the runner liveness line
    and a pointer to the latest run-all log dir so an operator can land
    on the right log file from one command without scraping the
    workspace tree.
    """
    workspace = workspace.resolve()
    entry = daemon_metadata(workspace)
    lines = [f"workspace: {workspace}"]
    if entry is None or entry.get("status") != "running":
        lines.append("daemon_status: stopped")
    else:
        lines.append("daemon_status: running")
        lines.append(f"pid: {entry.get('pid')}")
        lines.append(f"started_at: {entry.get('started_at')}")
        lines.append(f"log_dir: {entry.get('log_dir')}")
    runner = runner_status(workspace)
    state = load_state(workspace)
    lines.append(render_runner_status_line(runner, state))
    latest_dir = latest_run_all_log_dir(workspace)
    if latest_dir is not None:
        latest_dir_label = latest_dir
    else:
        latest_dir_label = "-"
    lines.append(f"latest_run_all_dir: {latest_dir_label}")
    latest_run = latest_matching(latest_dir, "*-run.log")
    if latest_run is not None:
        latest_run_label = latest_run
    else:
        latest_run_label = "-"
    lines.append(f"latest_run_log: {latest_run_label}")
    return lines
