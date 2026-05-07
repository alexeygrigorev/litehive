"""
Daemon loop body, foreground/background start, stop, and status helpers.

The daemon's job is to keep spawning ``litehive run`` until the pool
runs out of work or hits a halt condition. This module owns the loop
itself (``run_daemon_loop``), the detach-and-register flow
(``start_background_daemon``), the SIGTERM/SIGKILL escalation
(``stop_workspace_daemon``), and the operator-facing status block
(``daemon_status_lines``). Heartbeats and registration go through
``daemon.registry``; log directory pruning goes through
``daemon.logs``.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import TextIO

from litehive.container import build_workspace
from litehive.config.model import DaemonConfig
from litehive.config.workspace import create_workspace
from litehive.attention import AttentionRepository
from litehive.domain.pool import PoolStopReason
from litehive.domain.task import WorkspaceState
from litehive.db.schema import apply_pending_migrations
from litehive.git.ops import check_origin_divergence
from litehive.observability.status import (
    collect_task_pipeline_status_for_workspace,
    render_runner_status_line,
    render_task_pipeline_status_lines,
)
from litehive.observability.venv_health import daemon_broken_venv_message, probe_broken_venv_executables
from litehive.state.backup import create_scheduled_workspace_backup
from litehive.state.persist import load_state_for_workspace, set_pool_stop_reason
from litehive.state.locking import runner_pid_is_alive, runner_status_for_workspace
from litehive.workspace import Workspace

from .logs import latest_matching, prune_run_all_log_dirs, latest_run_all_log_dir_for_workspace

from .registry import (
    daemon_metadata,
    get_workspace_daemon,
    register_daemon,
    touch_daemon,
    unregister_daemon,
)

logger = logging.getLogger(__name__)

_DAEMON_TRANSIENT_STOP_REASONS = frozenset(
    {
        PoolStopReason.QUEUE_EXHAUSTED,
        PoolStopReason.TASK_REQUEUED,
    }
)


@dataclass(frozen=True, slots=True)
class DaemonStatusSnapshot:
    """
    Status state plus rendered daemon-loop text for one observation.

    Built by ``_daemon_status_snapshot_for_workspace`` before and
    after each child ``litehive run`` invocation. Keeping the
    `WorkspaceState` object intact avoids converting domain state into
    a loose dictionary just so the daemon can inspect queue and stop
    fields.
    """

    state: WorkspaceState
    text: str


def _halt_for_origin_divergence(
    workspace: Path,
    attention_repository: AttentionRepository,
) -> str | None:
    """
    Stop the pool and flag attention when ``main`` has diverged from ``origin/main``.

    Returns the divergence message when the daemon loop should exit.
    Merging or rebasing a diverged ``main`` is a human decision: this
    helper persists ``diverged_from_origin`` and writes an
    attention-log entry, while the daemon loop owns how that fact is
    rendered to its output stream.
    """
    divergence_reason = check_origin_divergence(workspace)
    if divergence_reason is None:
        return None
    set_pool_stop_reason(workspace, "diverged_from_origin")
    attention_repository.append(divergence_reason)
    return divergence_reason


def sleep_with_stop(seconds: float, stop_requested_fn: Callable[[], bool]) -> None:
    """
    Pause the daemon loop without blocking operator stop requests.

    Called by ``run_daemon_loop`` when another runner already owns
    the workspace. The daemon signal handler flips that loop's local
    ``stop_requested`` flag, and the lambda passed here reads the
    flag between short sleeps so SIGTERM/SIGINT exits promptly instead
    of waiting for the whole idle interval.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or stop_requested_fn():
            return
        time.sleep(min(remaining, 1.0))


def _daemon_status_snapshot(workspace: Path) -> DaemonStatusSnapshot:
    """
    Build a daemon-loop status snapshot from a root path.

    Unit tests and older path-based helper code call this thin wrapper;
    the daemon loop itself uses `_daemon_status_snapshot_for_workspace`
    after it has already built a `Workspace`.
    """
    return _daemon_status_snapshot_for_workspace(build_workspace(workspace))


def _daemon_status_snapshot_for_workspace(workspace: Workspace) -> DaemonStatusSnapshot:
    """
    Capture pool state plus a renderable status block in one read-only pass.

    The daemon loop logs the snapshot before and after each
    ``litehive run`` invocation so the iteration log carries a
    before/after pair the operator can diff. ``read_only=True``
    keeps the snapshot from racing the runner's own writes — the
    daemon must not mutate state here, only observe it.
    """
    status = collect_task_pipeline_status_for_workspace(workspace, read_only=True)
    lines = render_task_pipeline_status_lines(status, workspace=workspace.root, mode="summary")
    return DaemonStatusSnapshot(state=status.state, text="\n".join(lines) + "\n")


def _emit(message: str, stream: TextIO | None) -> None:
    """
    Write a daemon-loop status line that survives interleaving with subprocess output.

    The trailing newline plus explicit flush is load-bearing: an
    operator running ``tail -f`` sees both daemon-loop messages and
    the subprocess's stdout interleaved, and a partial line would
    leave a half-printed daemon message that readers concatenate
    with the next ``litehive run`` line. ``stream is None`` is the
    silent path for non-interactive daemon mode so call sites don't
    each branch on it.
    """
    if stream is None:
        return
    stream.write(message)
    if not message.endswith("\n"):
        stream.write("\n")
    stream.flush()


def _heartbeat_age_seconds(heartbeat_at: object) -> float | None:
    """
    Compute how stale a recorded daemon heartbeat is, tolerating bad input.

    Called by `_daemon_healthcheck_failed` while starting a background
    daemon. It converts registry metadata into an age check without
    letting corrupt timestamps crash daemon startup.

    Returns ``None`` for missing/non-string/unparseable timestamps so
    the healthcheck treats them as "no recent heartbeat" rather than
    crashing the start-background path on a corrupt registry row.
    Naive timestamps are interpreted as UTC because pre-tzinfo daemon
    registrations stored them without tz, and we shouldn't refuse to
    start over a one-time format upgrade.
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


def _daemon_healthcheck_failed(entry: dict[str, object], config: DaemonConfig) -> bool:
    """
    Detect a wedged registered daemon that should be reclaimed.

    A registry row with ``status=running`` but a heartbeat older than
    the configured daemon health timeout is treated as dead so
    ``start_background_daemon`` can SIGKILL the leftover and start
    fresh — without this, an operator who Ctrl-C'd a previous daemon
    would be unable to start a new one until they manually cleared
    the registry.
    """
    heartbeat_age = _heartbeat_age_seconds(entry.get("heartbeat_at"))
    return heartbeat_age is None or heartbeat_age > config.health_timeout_seconds


def _wait_for_pid_exit(pid: int, timeout_seconds: float, poll_interval_seconds: float) -> bool:
    """
    Block until a foreign pid is gone or the deadline passes.

    The stop sequence owns the registry row, not the ``Popen`` handle
    — so ``process.wait`` is unavailable and we have to poll
    ``runner_pid_is_alive`` instead. Used by the SIGTERM/SIGKILL escalation
    in ``_terminate_recorded_daemon`` and ``_force_kill_recorded_daemon``.
    """
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        if not runner_pid_is_alive(pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, poll_interval_seconds))


def _clear_recorded_daemon(workspace: Path, pid: int) -> None:
    """
    Drop the daemon registration row once the pid has confirmed exit.

    Used by the SIGTERM/SIGKILL stop sequence after liveness checks
    prove the old process is gone.
    Pinning ``unregister_daemon`` by ``pid`` prevents the stop
    sequence from racing a newer daemon: if a fresh daemon registered
    for the same workspace while we were waiting for the previous one
    to die, we must not clear that newer registration.
    """
    unregister_daemon(workspace, pid=pid)


def _force_kill_recorded_daemon(workspace: Path, pid: int, config: DaemonConfig) -> None:
    """
    SIGKILL a daemon that ignored SIGTERM, then clear its registration.

    Reached either as the escalation step of ``_terminate_recorded_daemon``
    or directly from ``start_background_daemon`` when an existing
    registration row fails its heartbeat check. Raises if the process
    refuses to die so the caller does not falsely conclude the
    workspace is free for a new daemon to register.
    """
    if not runner_pid_is_alive(pid):
        _clear_recorded_daemon(workspace, pid=pid)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        _clear_recorded_daemon(workspace, pid=pid)
        return
    except PermissionError as exc:
        raise RuntimeError(f"failed to send SIGKILL to daemon pid={pid}: {exc}") from exc
    if not _wait_for_pid_exit(
        pid,
        timeout_seconds=config.force_kill_timeout_seconds,
        poll_interval_seconds=config.exit_poll_interval_seconds,
    ):
        raise RuntimeError(f"daemon pid={pid} did not exit after SIGKILL")
    _clear_recorded_daemon(workspace, pid=pid)


def _terminate_recorded_daemon(workspace: Path, pid: int, config: DaemonConfig) -> None:
    """
    Run the graceful-then-forceful stop sequence against a registered daemon.

    Sends SIGTERM first so the daemon's signal handler can flush its
    heartbeat thread and unregister cleanly; if the process is still
    alive after the configured daemon stop grace period we escalate to
    SIGKILL via ``_force_kill_recorded_daemon``. Called by
    ``stop_workspace_daemon`` (and the ``litehive stop`` shortcut)
    when the operator asks the workspace's daemon to stop.
    """
    if not runner_pid_is_alive(pid):
        _clear_recorded_daemon(workspace, pid=pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_recorded_daemon(workspace, pid=pid)
        return
    except PermissionError as exc:
        raise RuntimeError(f"failed to send SIGTERM to daemon pid={pid}: {exc}") from exc
    if _wait_for_pid_exit(
        pid,
        timeout_seconds=config.stop_grace_period_seconds,
        poll_interval_seconds=config.exit_poll_interval_seconds,
    ):
        _clear_recorded_daemon(workspace, pid=pid)
        return
    _force_kill_recorded_daemon(workspace, pid=pid, config=config)


def _terminate_child_process(process: subprocess.Popen[str]) -> None:
    """
    Forward a stop signal from the daemon to the whole ``litehive run`` subtree.

    The child runs in its own session (``start_new_session=True``)
    because it spawns subagents that themselves spawn subprocesses;
    sending SIGTERM only to the immediate child would orphan that
    subtree. ``killpg`` covers the whole group, with
    ``process.terminate`` as a fallback for platforms that lack
    ``killpg``.
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
    """
    True when another ``litehive run`` is still active for the workspace.

    Treats ``running`` and ``late`` as live (a late runner is still
    inside the heartbeat grace window); ``stale`` is excluded because
    that means we've already given up and the workspace is reclaimable.
    The daemon loop idles instead of spawning a duplicate runner when
    this returns true.
    """
    return getattr(status, "status", None) in {"running", "late"}


def _has_work(state: WorkspaceState) -> bool:
    """
    True when the pool still has something to do.

    "Something to do" = an active task in flight, or anything in the
    queue. The loop exits cleanly when this returns false so an empty
    queue doesn't keep the daemon spinning every second.
    """
    return state.active_task_id is not None or bool(state.queue)


def _daemon_should_continue_for_stop_reason(reason: str | None) -> bool:
    """
    Distinguish a transient pause from a halt the daemon should respect.

    Reasons that clear themselves on the next iteration
    (``queue_exhausted``, ``task_requeued``) keep the daemon looping
    so an operator who adds a task doesn't have to restart it. Every
    other reason (operator halts, ``diverged_from_origin``, dirty
    state) is honored — those require human action and looping would
    spin forever.
    """
    if reason is None:
        return True
    stop_reason = PoolStopReason.from_value(reason)
    if stop_reason is None:
        return False
    return stop_reason in _DAEMON_TRANSIENT_STOP_REASONS


def _emit_runner_wait(status, stream: TextIO | None) -> None:
    """
    Log enough about the live runner for the operator to diagnose the wait.

    The daemon loop reaches this branch when another ``litehive run``
    is still active for the workspace. Surfacing pid, active task,
    and last heartbeat lets the operator decide whether to wait or
    intervene; without it the daemon log would just say "idling"
    repeatedly with no context.
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


def create_workspace_venvs_ready(
    workspace: Path,
) -> None:
    """
    Refuse to start the daemon when the workspace has a broken Python venv.

    Called by `start_background_daemon` before forking the worker.
    A broken venv would surface inside subagents as cryptic
    ``ModuleNotFoundError``s on every task, with the operator chasing
    a different agent each time. Failing here produces exactly one
    clear error per broken workspace and stops a wave of confusing
    task failures from hitting the queue.
    """
    findings = probe_broken_venv_executables(workspace)
    if findings:
        raise RuntimeError(daemon_broken_venv_message(workspace, findings))


def maybe_run_workspace_backup(
    workspace: Path,
    *,
    now: datetime | None = None,
) -> str | None:
    """
    Trigger the scheduled workspace backup if one is due.

    The backup cadence policy lives in ``state.backup``; this hook is
    the only place in the daemon loop that calls it. Returns the
    created backup timestamp so the loop can decide whether and where
    to render it.
    """
    backup = create_scheduled_workspace_backup(workspace, now=now)
    if backup is None:
        return None
    return backup.timestamp


def run_logged_subprocess(
    command: list[str],
    cwd: Path,
    log_path: Path,
    output_stream: TextIO | None,
    current_child: dict[str, subprocess.Popen[str] | None],
) -> int:
    """
    Spawn a child ``litehive run`` and tee its output to a per-iteration log file.

    The child is started in its own process group so the daemon's
    SIGTERM handler can kill the whole subtree (subagents and any
    sub-subprocesses they spawned). The live ``Popen`` is published
    to ``current_child`` so that handler can reach it. Each output
    line is flushed twice (log file plus optional operator stream)
    so an operator running ``tail -f`` on the iteration log sees
    progress as it happens.
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
    """
    Drive a workspace's pool by repeatedly spawning ``litehive run`` until work runs out.

    The daemon worker body — invoked by ``daemon worker`` (the
    detached child of ``start_background_daemon``) and by the
    foreground ``daemon run`` for development. Owns the heartbeat
    thread that keeps the registration row alive, the per-iteration
    log files, signal forwarding to the active subprocess, and the
    stop-reason policy that decides when the pool is truly done
    versus only momentarily paused.
    """
    workspace = workspace.resolve()
    create_workspace(workspace)
    daemon_workspace = build_workspace(workspace)
    attention_repository = AttentionRepository(daemon_workspace)
    daemon_config = daemon_workspace.load_config().daemon
    apply_pending_migrations(workspace)
    command_prefix = [sys.executable, "-m", "litehive.main"]
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_base = daemon_workspace.runtime_path("logs", "run-all")
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
        args=(workspace, os.getpid(), heartbeat_stop, daemon_config.heartbeat_interval_seconds),
        daemon=True,
    )
    heartbeat_thread.start()

    def _handle_signal(signum: int, _frame: object) -> None:
        """
        SIGTERM/SIGINT handler for the daemon loop.

        Flips the local ``stop_requested`` flag so the loop exits
        after the current iteration, and forwards the signal to the
        live ``litehive run`` child via ``_terminate_child_process``
        so a Ctrl-C from the operator reaches the whole subtree
        instead of leaving a zombie subagent attached to a dead
        daemon.
        """
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

            live_runner = runner_status_for_workspace(daemon_workspace)
            if _runner_is_live(live_runner):
                _emit_runner_wait(live_runner, stream=output_stream)
                sleep_with_stop(1.0, stop_requested_fn=lambda: stop_requested)
                continue

            divergence_reason = _halt_for_origin_divergence(workspace, attention_repository)
            if divergence_reason is not None:
                _emit(
                    "!!! ATTENTION REQUIRED !!! Local main has diverged from origin/main. "
                    "Halting pool: diverged_from_origin",
                    stream=output_stream,
                )
                _emit(divergence_reason, stream=output_stream)
                return 0

            try:
                backup_timestamp = maybe_run_workspace_backup(workspace)
            except (OSError, RuntimeError) as exc:
                logger.exception("scheduled workspace backup failed")
                attention_repository.append(f"scheduled backup failed: {exc}")
                _emit(f"backup_failed: {exc}", stream=output_stream)
            else:
                if backup_timestamp is not None:
                    _emit(f"backup_created: {backup_timestamp}", stream=output_stream)

            try:
                pre_snapshot = _daemon_status_snapshot_for_workspace(daemon_workspace)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.exception("status snapshot raised")
                _emit(f"status raised: {exc}", stream=output_stream)
                return 1
            _emit(pre_snapshot.text, stream=output_stream)

            if not _has_work(pre_snapshot.state):
                _emit("No active or queued tasks remain. Stopping.", stream=output_stream)
                return 0
            stop_reason_before = pre_snapshot.state.pool_stop_reason
            if not _daemon_should_continue_for_stop_reason(stop_reason_before):
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
                post_snapshot = _daemon_status_snapshot_for_workspace(daemon_workspace)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.exception("post-status snapshot raised")
                _emit(f"post-status raised: {exc}", stream=output_stream)
                return 1
            _emit(post_snapshot.text, stream=output_stream)

            stop_reason = post_snapshot.state.pool_stop_reason
            if not _has_work(post_snapshot.state):
                _emit("No active or queued tasks remain. Stopping.", stream=output_stream)
                return 0
            if not _daemon_should_continue_for_stop_reason(stop_reason):
                _emit(f"Runner stopped: {stop_reason}", stream=output_stream)
                return 0
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=max(daemon_config.heartbeat_interval_seconds, 0.1) * 2)
        unregister_daemon(workspace, pid=os.getpid())


def _daemon_heartbeat_loop(
    workspace: Path,
    pid: int,
    stop_event: threading.Event,
    interval_seconds: float,
) -> None:
    """
    Refresh the daemon registration's heartbeat row while the worker is alive.

    Runs in a background thread because a single ``litehive run``
    iteration can take minutes and would otherwise stall the
    main-thread heartbeat past the configured health timeout,
    making the daemon look dead to ``_daemon_healthcheck_failed``.
    Exits as soon as the run-loop sets ``stop_event`` during
    shutdown.
    """
    while not stop_event.wait(interval_seconds):
        touch_daemon(workspace, pid=pid)


def start_background_daemon(workspace: Path) -> int:
    """
    Detach a daemon worker for ``workspace`` and return once it has registered.

    Reclaims a wedged registration (status running but missed its
    heartbeat) so an operator who Ctrl-C'd a previous daemon can
    re-run ``litehive daemon start`` without manual cleanup. Waits
    for the child to write its registration row before returning,
    so callers can rely on the registry being current — without the
    wait, an immediate ``litehive status`` call would race the
    daemon's first heartbeat write.
    """
    workspace = workspace.resolve()
    daemon_config = build_workspace(workspace).load_config().daemon
    existing = daemon_metadata(workspace)
    if existing is not None and existing.get("status") == "running":
        pid = existing.get("pid")
        # Registry metadata is read from SQLite/JSON and must be narrowed
        # before using it as an OS pid.
        if isinstance(pid, int) and _daemon_healthcheck_failed(existing, daemon_config):
            _force_kill_recorded_daemon(workspace, pid=pid, config=daemon_config)
        else:
            raise RuntimeError(f"daemon already running for {workspace}: pid={pid}")
    if existing is not None and existing.get("status") == "stale":
        unregister_daemon(workspace)
    create_workspace_venvs_ready(workspace)
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
    deadline = time.monotonic() + daemon_config.startup_timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("daemon failed to start")
        entry = get_workspace_daemon(workspace)
        if entry is not None and entry.get("pid") == process.pid:
            return process.pid
        time.sleep(daemon_config.startup_poll_interval_seconds)
    raise RuntimeError("daemon did not register before timeout")


def stop_workspace_daemon(workspace: Path) -> dict[str, object] | None:
    """
    Stop the daemon registered for ``workspace``, returning its prior registration.

    Called by ``litehive daemon stop`` and the ``litehive stop``
    shortcut. Returns ``None`` when there was no daemon to stop so
    the CLI can distinguish "nothing to do" from "stopped a running
    daemon" without a second registry round-trip. Also drops stale
    registrations as a side effect, so leftover rows from prior
    crashes do not survive an explicit stop.
    """
    workspace = workspace.resolve()
    daemon_config = build_workspace(workspace).load_config().daemon
    entry = daemon_metadata(workspace)
    if entry is None:
        return None
    if entry.get("status") != "running":
        unregister_daemon(workspace)
        return None
    pid = entry.get("pid")
    # Registry metadata is read from SQLite/JSON and must be narrowed
    # before using it as an OS pid.
    if not isinstance(pid, int):
        unregister_daemon(workspace)
        return None
    _terminate_recorded_daemon(workspace, pid=pid, config=daemon_config)
    return entry


def daemon_status_lines(workspace: Path) -> list[str]:
    """
    Render daemon status lines from a root path.

    The CLI status command still passes a path at this boundary; this
    wrapper builds the `Workspace` once and delegates to
    `daemon_status_lines_for_workspace`.
    """
    return daemon_status_lines_for_workspace(build_workspace(workspace))


def daemon_status_lines_for_workspace(workspace: Workspace) -> list[str]:
    """
    Render the daemon-side block of ``litehive status`` / ``litehive daemon status``.

    Combines the daemon registration row, the runner liveness line,
    and a pointer to the latest run-all log directory so the operator
    can land on the right log file from one command without scraping
    the workspace tree. Failing to surface the latest log dir here
    is the difference between "I can debug" and "I have to grep".
    """
    root = workspace.root
    entry = daemon_metadata(root)
    lines = [f"workspace: {root}"]
    if entry is None or entry.get("status") != "running":
        lines.append("daemon_status: stopped")
    else:
        lines.append("daemon_status: running")
        lines.append(f"pid: {entry.get('pid')}")
        lines.append(f"started_at: {entry.get('started_at')}")
        lines.append(f"log_dir: {entry.get('log_dir')}")
    runner = runner_status_for_workspace(workspace)
    state = load_state_for_workspace(workspace)
    lines.append(render_runner_status_line(runner, state))
    latest_dir = latest_run_all_log_dir_for_workspace(workspace)
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
