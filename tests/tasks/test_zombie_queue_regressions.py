import subprocess
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.workspace import create_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.outcomes import TaskOutcomeKind
from litehive.git.ops import has_non_litehive_changes
from litehive.state.persist import load_state_for_workspace, save_state_for_workspace
from litehive.state.records import (
    create_task_for_workspace,
    get_task_for_workspace,
    require_task_for_workspace,
    save_task_for_workspace,
)
from litehive.tasks.queue import dequeue_next_task_selection, peek_next_task_selection
from litehive.workspace import Workspace


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_ok(cwd: Path, *args: str) -> str:
    proc = _git(cwd, *args)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.strip() or proc.stdout.strip() or "git command failed")
    return proc.stdout.strip()


def _configure_repo(path: Path) -> None:
    _git_ok(path, "config", "user.email", "test@example.com")
    _git_ok(path, "config", "user.name", "Test User")


def _queue_task(
    workspace: Workspace,
    task_id: str,
    *,
    status: str = "queued",
    pipeline_status: str = "implementing",
    execution_status: str = "idle",
    outcome_kind: TaskOutcomeKind | None = None,
) -> None:
    task = require_task_for_workspace(workspace, task_id)
    task.status = TaskStatus(status)
    task.pipeline_status = PipelineStatus(pipeline_status)
    task.runtime.pipeline.execution_status = execution_status
    task.runtime.pipeline.last_outcome.kind = outcome_kind
    save_task_for_workspace(workspace, task)


def test_dequeue_filter_skips_terminal_runtime_execution_statuses(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    zombie_ids: list[str] = []
    for execution_status in ("done", "cancelled", "failed", "blocked", "interrupted"):
        zombie = create_task_for_workspace(workspace, title=f"Zombie {execution_status}")
        zombie_ids.append(zombie.id)
        _queue_task(
            workspace,
            zombie.id,
            pipeline_status="implementing",
            execution_status=execution_status,
        )
    runnable = create_task_for_workspace(workspace, title="Runnable")

    state = load_state_for_workspace(workspace)
    state.queue = [*zombie_ids, runnable.id]
    save_state_for_workspace(workspace, state)

    selection = dequeue_next_task_selection(workspace)

    assert selection.task is not None
    assert selection.task.id == runnable.id

    refreshed_state = load_state_for_workspace(workspace)
    assert refreshed_state.active_task_id == runnable.id
    assert refreshed_state.queue == []


def test_queued_task_with_done_execution_status_is_not_selected(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    zombie = create_task_for_workspace(workspace, title="Done zombie")
    runnable = create_task_for_workspace(workspace, title="Runnable")
    _queue_task(
        workspace,
        zombie.id,
        pipeline_status="implementing",
        execution_status="done",
    )

    state = load_state_for_workspace(workspace)
    state.queue = [zombie.id, runnable.id]
    save_state_for_workspace(workspace, state)

    selection = dequeue_next_task_selection(workspace)

    assert selection.task is not None
    assert selection.task.id == runnable.id


def test_dequeue_selection_skips_tasks_missing_runtime_rows(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    legacy = create_task_for_workspace(workspace, title="Legacy disk-only task")
    runnable = create_task_for_workspace(workspace, title="Runnable")

    with connect_workspace_db(tmp_path) as connection:
        connection.execute("DELETE FROM task_state WHERE task_id = ?", (legacy.id,))

    state = load_state_for_workspace(workspace)
    state.queue = [legacy.id, runnable.id]
    save_state_for_workspace(workspace, state)

    selection = dequeue_next_task_selection(workspace)

    assert selection.task is not None
    assert selection.task.id == runnable.id

    refreshed_state = load_state_for_workspace(workspace)
    assert refreshed_state.active_task_id == runnable.id
    assert refreshed_state.queue == [legacy.id]


def test_dequeue_filter_skips_terminal_last_outcome_kinds(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    zombie_ids: list[str] = []
    for outcome_kind in (TaskOutcomeKind.DUPLICATE, TaskOutcomeKind.DEFERRED, TaskOutcomeKind.WONT_DO):
        zombie = create_task_for_workspace(workspace, title=f"Zombie {outcome_kind.value}")
        zombie_ids.append(zombie.id)
        _queue_task(
            workspace,
            zombie.id,
            pipeline_status="implementing",
            outcome_kind=outcome_kind,
        )
    runnable = create_task_for_workspace(workspace, title="Runnable")

    state = load_state_for_workspace(workspace)
    state.queue = [*zombie_ids, runnable.id]
    save_state_for_workspace(workspace, state)

    selection = dequeue_next_task_selection(workspace)

    assert selection.task is not None
    assert selection.task.id == runnable.id

    refreshed_state = load_state_for_workspace(workspace)
    assert refreshed_state.active_task_id == runnable.id
    assert refreshed_state.queue == []


def test_selector_resets_stale_queued_pipeline_status_to_backlog(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Stale stage")
    _queue_task(
        workspace,
        task.id,
        pipeline_status="implementing",
        execution_status="idle",
    )

    state = load_state_for_workspace(workspace)
    state.queue = [task.id]
    save_state_for_workspace(workspace, state)

    selection = peek_next_task_selection(workspace)

    assert selection.task is not None
    assert selection.task.id == task.id

    refreshed = require_task_for_workspace(workspace, task.id)
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.pipeline.current_stage.stage == "backlog"
    assert refreshed.runtime.pipeline.current_stage.status == "idle"


def test_run_drain_skips_zombie_queue_entries_and_leaves_main_clean(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _git_ok(workspace_root, "init", "-b", "main")
    _configure_repo(workspace_root)
    create_workspace(workspace_root)
    workspace = Workspace.from_path(workspace_root)

    (workspace_root / "tracked.txt").write_text("seed\n", encoding="utf-8")
    _git_ok(workspace_root, "add", "tracked.txt")
    _git_ok(workspace_root, "commit", "-m", "initial")

    done_task = create_task_for_workspace(workspace, title="Done zombie")
    cancelled_task = create_task_for_workspace(workspace, title="Cancelled zombie")
    duplicate_task = create_task_for_workspace(workspace, title="Duplicate zombie")
    deferred_task = create_task_for_workspace(workspace, title="Deferred zombie")
    wont_do_task = create_task_for_workspace(workspace, title="Won't do zombie")

    _queue_task(workspace, done_task.id, pipeline_status="implementing", execution_status="done")
    _queue_task(workspace, cancelled_task.id, pipeline_status="testing", execution_status="cancelled")
    _queue_task(workspace, duplicate_task.id, pipeline_status="accepting", outcome_kind=TaskOutcomeKind.DUPLICATE)
    _queue_task(workspace, deferred_task.id, pipeline_status="grooming", outcome_kind=TaskOutcomeKind.DEFERRED)
    _queue_task(workspace, wont_do_task.id, pipeline_status="commit_to_git", outcome_kind=TaskOutcomeKind.WONT_DO)

    state = load_state_for_workspace(workspace)
    state.queue = [
        done_task.id,
        cancelled_task.id,
        duplicate_task.id,
        deferred_task.id,
        wont_do_task.id,
    ]
    save_state_for_workspace(workspace, state)

    called = {"count": 0}

    def _unexpected_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        called["count"] += 1
        (workspace_root / "orphan.txt").write_text("should not exist\n", encoding="utf-8")
        task = args[1]
        return SimpleNamespace(task=task, final_stage="done", failed_reason=None, failed_message=None)

    monkeypatch.setattr("litehive.cli.runner.run_task", _unexpected_run)

    result = CliRunner().invoke(
        app,
        ["run", "--drain", "--workspace", str(workspace_root)],
        standalone_mode=False,
    )

    assert result.return_value == 0
    assert called["count"] == 0
    assert not (workspace_root / "orphan.txt").exists()

    refreshed_state = load_state_for_workspace(workspace)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue == []
    assert has_non_litehive_changes(workspace_root) is False


def test_task_with_hook_rejection_and_recovery_has_terminal_execution_status(tmp_path: Path) -> None:
    """Regression test for T-0455: task that experiences hook rejection and later passes should have execution_status="done"."""
    import pytest

    pytest.importorskip("litehive.lifecycle.orchestration")  # Skip if import fails

    from litehive.config.model import LitehiveConfig
    from litehive.config.workspace_files import config_path
    from litehive.lifecycle.nodes.agent import AgentVerdict
    from litehive.lifecycle.orchestration import run_task as run_pipeline_task
    import yaml

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _git_ok(workspace_root, "init", "-b", "main")
    _configure_repo(workspace_root)
    create_workspace(workspace_root)
    workspace = Workspace.from_path(workspace_root)

    # Create initial commit
    (workspace_root / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git_ok(workspace_root, "add", "tracked.txt")
    _git_ok(workspace_root, "commit", "-m", "initial commit")

    task = create_task_for_workspace(workspace, title="Hook rejection and recovery test", pipeline_mode="single")

    # Configure hook that fails twice then passes (based on successful pattern in test_hook_reject_circuit_breaker.py)
    def _fail_twice_then_pass_command(task_id: str) -> str:
        return (
            f'if [ "$LITEHIVE_TASK_ID" != "{task_id}" ]; then exit 0; fi; '
            "count=$(cat .hook_reject_count 2>/dev/null || echo 0); "
            "count=$((count + 1)); "
            'printf "%s" "$count" > .hook_reject_count; '
            'if [ "$count" -le 2 ]; then '
            'echo "Hook reject attempt $count" >&2; '
            "exit 1; "
            "fi; "
            "exit 0"
        )

    config = LitehiveConfig(
        runner_hooks={
            "after_implementing": [
                {
                    "command": _fail_twice_then_pass_command(task.id),
                    "description": "Test hook that fails twice then passes",
                }
            ]
        }
    )

    config_file = config_path(workspace_root)
    config_file.write_text(yaml.dump(asdict(config)), encoding="utf-8")

    # Mock engine that passes on all attempts
    class MockEngine:
        def __init__(self, name: str) -> None:
            self.name = name

        def run_turn(self, session, prompt, state) -> AgentVerdict:
            del session  # unused
            role = getattr(prompt, "role", "unknown")

            if role == "swe":
                # SWE agent implementation - create some change and pass
                test_file = workspace_root / "test_implementation.txt"
                test_file.write_text("Implementation completed\n", encoding="utf-8")
                return AgentVerdict(outcome="pass")
            else:
                # All other roles (including recovery) pass
                return AgentVerdict(outcome="pass")

    # Run the task through the pipeline
    result = run_pipeline_task(
        workspace_root,
        task,
        engine_factory=lambda engine_name: MockEngine(engine_name),
    )

    # Verify the task completed successfully
    assert result.final_stage == "done"

    # Verify the task has the correct terminal state - this is the key regression test
    completed_task = get_task_for_workspace(workspace, task.id)
    assert completed_task is not None
    assert completed_task.status == "done"
    assert completed_task.pipeline_status == "done"
    assert completed_task.runtime.pipeline.execution_status == "done"  # This is the key fix

    # Verify that the completed task is not eligible for execution
    from litehive.tasks.queue import _has_terminal_execution_status, is_task_eligible_for_execution

    assert _has_terminal_execution_status(completed_task) is True
    assert is_task_eligible_for_execution(completed_task) is False

    # Additional verification: create a simple queue test to confirm it would be skipped
    # Create a fresh runnable task for comparison
    runnable_task = create_task_for_workspace(workspace, title="Runnable task")

    # Manually set up a scenario to test queue selection
    _queue_task(workspace, completed_task.id, status="done", pipeline_status="done", execution_status="done")

    state = load_state_for_workspace(workspace)
    state.queue = [completed_task.id, runnable_task.id]
    save_state_for_workspace(workspace, state)

    # Verify queue selection correctly skips the completed task
    selection = dequeue_next_task_selection(workspace)
    assert selection.task is not None
    assert selection.task.id == runnable_task.id
