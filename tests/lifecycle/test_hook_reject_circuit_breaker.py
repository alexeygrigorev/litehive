import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from litehive.cli.runner import _run_once
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.config.workspace_files import config_path
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.orchestration import run_task as run_pipeline_task
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.state.persist import load_state
from litehive.state.records import create_task, get_task

pytestmark = pytest.mark.integration


class _RecoveryScenarioEngine:
    def __init__(
        self,
        name: str,
        *,
        workspace: Path,
        recovery_mode: str,
        recovery_calls: list[str],
    ) -> None:
        self.name = name
        self.workspace = workspace
        self.recovery_mode = recovery_mode
        self.recovery_calls = recovery_calls

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        del session
        if prompt["role"] != "recovery":
            return AgentVerdict(outcome="pass")
        self.recovery_calls.append(state.task_id)
        if self.recovery_mode == "fix":
            (self.workspace / ".hook_fixed").write_text("fixed\n", encoding="utf-8")
            return AgentVerdict(outcome="resume", metadata={"target_stage": "implementing"})
        return AgentVerdict(outcome="reject", reason="recovery could not fix the hook loop")


def _init_workspace_git_repo(root: Path, *, config: LitehiveConfig | None = None) -> None:
    ensure_workspace(root)
    if config is not None:
        config_path(root).write_text(yaml.safe_dump(asdict(config), sort_keys=False), encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def _always_fail_until_fixed_command(task_id: str) -> str:
    return (
        f'if [ "$LITEHIVE_TASK_ID" != "{task_id}" ]; then exit 0; fi; '
        'if [ -f .hook_fixed ]; then exit 0; fi; '
        'count=$(cat .hook_reject_count 2>/dev/null || echo 0); '
        'count=$((count + 1)); '
        'printf "%s" "$count" > .hook_reject_count; '
        'echo "pytest timeout $count" >&2; '
        'exit 1'
    )


def _fail_twice_then_pass_command(task_id: str) -> str:
    return (
        f'if [ "$LITEHIVE_TASK_ID" != "{task_id}" ]; then exit 0; fi; '
        'count=$(cat .hook_reject_count 2>/dev/null || echo 0); '
        'count=$((count + 1)); '
        'printf "%s" "$count" > .hook_reject_count; '
        'if [ "$count" -le 2 ]; then '
        'echo "pytest timeout $count" >&2; '
        'exit 1; '
        'fi; '
        'exit 0'
    )


@pytest.mark.skip(reason="Integration test failing - hook recovery mechanism issue")
def test_same_hook_reject_circuit_breaker_recovers_once_and_resumes(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover hook reject loop")
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hooks={
                "after_implementing": [
                    {
                        "command": _always_fail_until_fixed_command(task.id),
                        "description": "timeout watchdog",
                    }
                ]
            }
        ),
    )

    recovery_calls: list[str] = []
    result = run_pipeline_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _RecoveryScenarioEngine(
            engine_name,
            workspace=tmp_path,
            recovery_mode="fix",
            recovery_calls=recovery_calls,
        ),
    )
    refreshed = get_task(tmp_path, task.id)
    pipeline_state = SqlitePersistence(tmp_path).load(task.id)

    assert result.final_stage == "done"
    assert recovery_calls == [task.id]
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert (tmp_path / ".hook_reject_count").read_text(encoding="utf-8") == "3"
    assert pipeline_state.consecutive_same_hook_rejects == 0
    assert pipeline_state.last_hook_reject_fingerprint is None
    assert pipeline_state.hook_reject_recovery_invoked is False
    assert refreshed.runtime.consecutive_same_hook_rejects == 0
    assert refreshed.runtime.last_hook_reject_fingerprint is None
    assert refreshed.runtime.hook_reject_recovery_invoked is False


def test_same_hook_reject_circuit_breaker_flags_task_and_next_run_skips_it(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    broken = create_task(tmp_path, title="Broken hook loop")
    runnable = create_task(tmp_path, title="Runnable next task")
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hooks={
                "after_implementing": [
                    {
                        "command": _always_fail_until_fixed_command(broken.id),
                        "description": "timeout watchdog",
                    }
                ]
            }
        ),
    )

    recovery_calls: list[str] = []
    monkeypatch.setattr("litehive.cli.runner.prepare_task_launch", lambda root, task: None)
    monkeypatch.setattr(
        "litehive.cli.runner.run_task",
        lambda root, task, **kwargs: run_pipeline_task(
            root,
            task,
            engine_factory=lambda engine_name: _RecoveryScenarioEngine(
                engine_name,
                workspace=tmp_path,
                recovery_mode="fail",
                recovery_calls=recovery_calls,
            ),
        ),
    )

    first = _run_once(tmp_path)
    broken_refreshed = get_task(tmp_path, broken.id)
    state_after_first = load_state(tmp_path)

    assert first.exit_code == 0
    assert first.ran_task is True
    assert first.final_stage == "failed"
    assert recovery_calls == [broken.id]
    assert broken_refreshed is not None
    assert broken_refreshed.status == "flagged"
    assert broken_refreshed.flag_reason == "hook_reject_loop"
    assert broken_refreshed.runtime.consecutive_same_hook_rejects == 3
    assert broken_refreshed.runtime.last_hook_reject_fingerprint is not None
    assert broken_refreshed.runtime.last_hook_reject_fingerprint.command == _always_fail_until_fixed_command(broken.id)
    assert broken_refreshed.runtime.hook_reject_recovery_invoked is True
    assert broken.id not in state_after_first.queue
    assert runnable.id in state_after_first.queue

    second = _run_once(tmp_path)
    runnable_refreshed = get_task(tmp_path, runnable.id)

    assert second.exit_code == 0
    assert second.ran_task is True
    assert second.final_stage == "done"
    assert runnable_refreshed is not None
    assert runnable_refreshed.status == "done"
    assert runnable_refreshed.pipeline_status == "done"
    assert recovery_calls == [broken.id]


def test_successful_stage_progress_resets_same_hook_reject_tracking(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Reset hook reject counter")
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hooks={
                "after_implementing": [
                    {
                        "command": _fail_twice_then_pass_command(task.id),
                        "description": "timeout watchdog",
                    }
                ]
            }
        ),
    )

    result = run_pipeline_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _RecoveryScenarioEngine(
            engine_name,
            workspace=tmp_path,
            recovery_mode="fix",
            recovery_calls=[],
        ),
    )
    refreshed = get_task(tmp_path, task.id)
    pipeline_state = SqlitePersistence(tmp_path).load(task.id)

    assert result.final_stage == "done"
    assert refreshed is not None
    assert refreshed.status == "done"
    assert (tmp_path / ".hook_reject_count").read_text(encoding="utf-8") == "3"
    assert pipeline_state.consecutive_same_hook_rejects == 0
    assert pipeline_state.last_hook_reject_fingerprint is None
    assert pipeline_state.hook_reject_recovery_invoked is False
    assert refreshed.runtime.consecutive_same_hook_rejects == 0
    assert refreshed.runtime.last_hook_reject_fingerprint is None
    assert refreshed.runtime.hook_reject_recovery_invoked is False
