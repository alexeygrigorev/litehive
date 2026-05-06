import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

import heru
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.paths import workspace_path
from litehive.config.workspace import ensure_workspace
from litehive.domain.task import UnmergedWorktree
from litehive.state.locking import runner_lock_is_held
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, get_task, save_task
from litehive.worktree.paths import serialize_worktree_path, task_worktree_branch
from litehive.domain.common import PipelineStatus, TaskStatus

_RUNNER = CliRunner()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HERU_ROOT = Path(heru.__file__).resolve().parents[1]
_FAKE_RUNNER_SCRIPT = textwrap.dedent(
    """
    import signal
    import threading
    import sys
    from pathlib import Path

    from litehive.state.locking import runner_heartbeat, workspace_runner_guard

    root = Path(sys.argv[1])
    active_task_id = sys.argv[2]
    ready_file = Path(sys.argv[3])
    stop = threading.Event()

    def _handle_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    with workspace_runner_guard(root):
        with runner_heartbeat(root, active_task_id=active_task_id, interval_seconds=0.05):
            ready_file.write_text("ready\\n", encoding="utf-8")
            while not stop.wait(0.05):
                pass
    """
)


def _git_ok(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.strip() or proc.stdout.strip() or "git command failed")
    return proc.stdout.strip()


def _bootstrap_git_workspace(workspace: Path) -> None:
    _git_ok(workspace, "init", "-b", "main")
    _git_ok(workspace, "config", "user.email", "test@example.com")
    _git_ok(workspace, "config", "user.name", "Test User")
    ensure_workspace(workspace)
    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "-A")
    _git_ok(workspace, "commit", "-m", "initial")


def _create_merge_failed_worktree_task(workspace: Path):
    task = create_task(workspace, title="Rescue me", auto_commit=False)
    worktree_path = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _git_ok(workspace, "worktree", "add", "-b", task_worktree_branch(task), str(worktree_path), "HEAD")
    (worktree_path / "feature.txt").write_text("rescued\n", encoding="utf-8")
    _git_ok(worktree_path, "add", "feature.txt")
    _git_ok(worktree_path, "commit", "-m", "feature commit")

    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.FLAGGED
    task.flag_reason = "merge_failed"
    task.runtime.pipeline.git.worktree_path = serialize_worktree_path(worktree_path)
    task.git.worktree_path = None
    save_task(workspace, task)

    state = load_state(workspace)
    state.unmerged_worktrees = [UnmergedWorktree(task_id=task.id, worktree_path=str(worktree_path))]
    save_state(workspace, state)
    return task, worktree_path


def _flag_for_rescue(workspace: Path, task, worktree_path: Path) -> None:
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.FLAGGED
    task.flag_reason = "merge_failed"
    task.runtime.pipeline.git.worktree_path = serialize_worktree_path(worktree_path)
    task.git.worktree_path = None
    save_task(workspace, task)

    state = load_state(workspace)
    state.unmerged_worktrees = [UnmergedWorktree(task_id=task.id, worktree_path=str(worktree_path))]
    save_state(workspace, state)


def _spawn_fake_runner(workspace: Path, *, active_task_id: str, ready_file: Path) -> subprocess.Popen[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(_REPO_ROOT), str(_HERU_ROOT)]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return subprocess.Popen(
        [sys.executable, "-c", _FAKE_RUNNER_SCRIPT, str(workspace), active_task_id, str(ready_file)],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_runner_lock(proc: subprocess.Popen[str], workspace: Path, ready_file: Path) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        returncode = proc.poll()
        if returncode is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(
                "fake runner exited before acquiring the runner lock "
                f"(returncode={returncode}, stdout={stdout!r}, stderr={stderr!r})"
            )
        if ready_file.exists() and runner_lock_is_held(workspace):
            return
        time.sleep(0.05)
    raise AssertionError("fake runner did not acquire the runner lock within 15s")


def _stop_runner(proc: subprocess.Popen[str], workspace: Path) -> None:
    try:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
    finally:
        assert not runner_lock_is_held(workspace)


def test_worktree_rescue_apply_completes_while_another_runner_holds_the_lock(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _bootstrap_git_workspace(workspace)
    rescue_task, _ = _create_merge_failed_worktree_task(workspace)

    busy_task = create_task(workspace, title="Busy runner", auto_commit=False)
    busy_task.status = TaskStatus.IN_PROGRESS
    busy_task.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task(workspace, busy_task)

    state = load_state(workspace)
    state.active_task_id = busy_task.id
    save_state(workspace, state)

    ready_file = tmp_path / "runner-ready"
    proc = _spawn_fake_runner(workspace, active_task_id=busy_task.id, ready_file=ready_file)
    try:
        _wait_for_runner_lock(proc, workspace, ready_file)
        result = _RUNNER.invoke(
            app,
            ["worktree", "rescue", "--apply", "--workspace", str(workspace)],
            standalone_mode=False,
        )

        assert result.exit_code == 0
        assert result.return_value == 0
        assert "status: clean" in result.output
        assert "clean_count: 1" in result.output
        assert "active_task_count: 0" in result.output
        assert (workspace / "feature.txt").read_text(encoding="utf-8") == "rescued\n"

        refreshed = get_task(workspace, rescue_task.id)
        assert refreshed is not None
        assert refreshed.status == "done"
        assert refreshed.pipeline_status == "done"
        assert refreshed.runtime.pipeline.git.worktree_path is None
        assert refreshed.runtime.pipeline.git.commit_sha == _git_ok(workspace, "rev-parse", "HEAD")

        refreshed_state = load_state(workspace)
        assert refreshed_state.active_task_id == busy_task.id
        assert all(entry.task_id != rescue_task.id for entry in refreshed_state.unmerged_worktrees)
    finally:
        _stop_runner(proc, workspace)


def test_worktree_rescue_apply_reports_missing_worktree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _bootstrap_git_workspace(workspace)

    task = create_task(workspace, title="Missing rescue", auto_commit=False)
    missing_worktree = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"
    _flag_for_rescue(workspace, task, missing_worktree)

    result = _RUNNER.invoke(
        app,
        ["worktree", "rescue", "--apply", "--workspace", str(workspace)],
        standalone_mode=False,
    )

    assert result.exit_code == 0
    assert result.return_value == 1
    assert "status: missing_worktree" in result.output
    assert "message: recorded worktree is missing" in result.output
    assert "missing_worktree_count: 1" in result.output


def test_worktree_rescue_apply_reconciles_already_landed_patch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _bootstrap_git_workspace(workspace)

    task = create_task(workspace, title="Already landed", auto_commit=False)
    worktree_path = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _git_ok(workspace, "worktree", "add", "-b", task_worktree_branch(task), str(worktree_path), "HEAD")
    (worktree_path / "feature.txt").write_text("same patch\n", encoding="utf-8")
    _git_ok(worktree_path, "add", "feature.txt")
    _git_ok(worktree_path, "commit", "-m", "feature commit")

    (workspace / "feature.txt").write_text("same patch\n", encoding="utf-8")
    _git_ok(workspace, "add", "feature.txt")
    _git_ok(workspace, "commit", "-m", "land equivalent patch")
    main_head = _git_ok(workspace, "rev-parse", "HEAD")
    _flag_for_rescue(workspace, task, worktree_path)

    result = _RUNNER.invoke(
        app,
        ["worktree", "rescue", "--apply", "--workspace", str(workspace)],
        standalone_mode=False,
    )

    assert result.exit_code == 0
    assert result.return_value == 0
    assert "status: already_landed" in result.output
    assert "message: worktree patch already landed on main" in result.output
    assert "already_landed_count: 1" in result.output

    refreshed = get_task(workspace, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.runtime.pipeline.git.worktree_path is None
    assert refreshed.runtime.pipeline.git.commit_sha == main_head


def test_worktree_rescue_apply_keeps_manual_conflict_pending(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _bootstrap_git_workspace(workspace)

    task = create_task(workspace, title="Manual conflict", auto_commit=False)
    worktree_path = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _git_ok(workspace, "worktree", "add", "-b", task_worktree_branch(task), str(worktree_path), "HEAD")
    (worktree_path / "app.txt").write_text("base\nworktree change\n", encoding="utf-8")
    _git_ok(worktree_path, "add", "app.txt")
    _git_ok(worktree_path, "commit", "-m", "worktree conflicting change")

    (workspace / "app.txt").write_text("base\nmain change\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "main conflicting change")
    _flag_for_rescue(workspace, task, worktree_path)

    result = _RUNNER.invoke(
        app,
        ["worktree", "rescue", "--apply", "--workspace", str(workspace)],
        standalone_mode=False,
    )

    assert result.exit_code == 0
    assert result.return_value == 1
    assert "status: manual_conflict" in result.output
    assert "manual_conflict_count: 1" in result.output
    assert not (workspace / ".git" / "CHERRY_PICK_HEAD").exists()

    refreshed = get_task(workspace, task.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.runtime.pipeline.git.worktree_path == serialize_worktree_path(worktree_path)


def test_worktree_rescue_apply_refuses_to_race_the_active_task(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _bootstrap_git_workspace(workspace)
    rescue_task, worktree_path = _create_merge_failed_worktree_task(workspace)

    state = load_state(workspace)
    state.active_task_id = rescue_task.id
    save_state(workspace, state)

    ready_file = tmp_path / "runner-ready"
    proc = _spawn_fake_runner(workspace, active_task_id=rescue_task.id, ready_file=ready_file)
    try:
        _wait_for_runner_lock(proc, workspace, ready_file)
        result = _RUNNER.invoke(
            app,
            ["worktree", "rescue", "--apply", "--workspace", str(workspace)],
            standalone_mode=False,
        )

        assert result.exit_code == 0
        assert result.return_value == 1
        assert "status: active_task" in result.output
        assert (
            f"message: task {rescue_task.id} is still state.active_task_id; "
            "worktree rescue refuses to race with the runner"
        ) in result.output
        assert "active_task_count: 1" in result.output
        assert not (workspace / "feature.txt").exists()

        refreshed = get_task(workspace, rescue_task.id)
        assert refreshed is not None
        assert refreshed.status == "flagged"
        assert refreshed.pipeline_status == "flagged"
        assert refreshed.flag_reason == "merge_failed"
        assert refreshed.runtime.pipeline.git.worktree_path == serialize_worktree_path(worktree_path)
        assert refreshed.runtime.pipeline.git.commit_sha is None
    finally:
        _stop_runner(proc, workspace)
