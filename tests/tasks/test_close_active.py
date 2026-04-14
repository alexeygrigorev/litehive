import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.workspace import ensure_workspace
from heru.types import SubagentRef
from litehive.state.records import create_task, require_task
from litehive.tasks.paths import runner_lock_path, task_dir
from litehive.state.persist import load_state
from litehive.tasks.queue import set_active_task
from litehive.state.locking import runner_lock_is_held
from litehive.tasks.runtime import (
    mark_subagent_started,
    mark_task_run_started,
)


def test_cmd_close_task_stops_active_runner_and_closes_task(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.setattr("litehive.cli.agent_cli.block_if_agent", lambda: None)
    task = create_task(tmp_path, title="Kill bad run")
    mark_task_run_started(tmp_path, task)
    set_active_task(tmp_path, task.id)

    task = require_task(tmp_path, task.id)
    mark_subagent_started(
        tmp_path,
        task,
        SubagentRef(
            id="SA-0001",
            role="swe",
            engine="codex",
            status="running",
            path="subagents/SA-0001-swe",
        ),
    )

    ready_file = tmp_path / "runner-ready"
    script = """
import signal
import threading
import time
from pathlib import Path

from litehive.state.locking import runner_heartbeat, workspace_runner_guard

root = Path(sys.argv[1])
task_id = sys.argv[2]
ready = Path(sys.argv[3])
stop = threading.Event()

def _handle_signal(signum, frame):
    stop.set()

signal.signal(signal.SIGINT, _handle_signal)
with workspace_runner_guard(root):
    with runner_heartbeat(root, active_task_id=task_id, interval_seconds=0.05):
        ready.write_text("ready\\n", encoding="utf-8")
        while not stop.wait(0.05):
            pass
"""
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[1]
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{repo_root}{os.pathsep}{pythonpath}" if pythonpath else str(repo_root)
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import sys\n{script}", str(tmp_path), task.id, str(ready_file)],
        cwd=repo_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if ready_file.exists() and runner_lock_is_held(tmp_path):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("runner process did not acquire the lock")
        result = CliRunner().invoke(
            app,
            [
                "task",
                "close",
                task.id,
                "--workspace",
                str(tmp_path),
                "--outcome",
                "wont_do",
                "--reason",
                "bad direction",
            ],
            standalone_mode=False,
        )

        assert result.return_value == 0
        out = result.output
        assert f"task: {task.id} Kill bad run" in out
        assert "status: wont_do" in out
        assert "outcome: wont_do" in out

        proc.wait(timeout=5)
        assert proc.returncode in {0, -signal.SIGINT}
        assert not runner_lock_is_held(tmp_path)
        lock_text = runner_lock_path(tmp_path).read_text(encoding="utf-8")
        assert lock_text == ""

        refreshed = require_task(tmp_path, task.id)
        assert refreshed.status == "wont_do"
        assert refreshed.runtime.execution_status == "cancelled"
        assert refreshed.runtime.active_subagent is None
        assert refreshed.runtime.last_outcome.reason_code == "wont_do"
        assert refreshed.runtime.last_outcome.reason == "bad direction"

        state = load_state(tmp_path)
        assert state.active_task_id is None
        assert task.id not in state.queue

        journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
        assert "Task closed: wont_do. bad direction" in journal
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
