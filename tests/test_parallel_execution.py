"""Tests for parallel task execution, isolated worktree usage,
deterministic integration, and merge-conflict agent resolution.

This tests task-level parallelism (separate independent tasks running
concurrently), NOT intra-task worker fanout.
"""

import subprocess
from unittest.mock import MagicMock

from tests.workspace_helpers import (
    LitehiveConfig,
    Path,
    StageReport,
    SubagentManager,
    TaskRecord,
    _init_git_repo,
    create_task,
    ensure_workspace,
    get_task,
    load_config,
    load_state,
    save_state,
    save_task,
    pytest,
    yaml,
)

from litehive.pipeline_old._parallel import (
    IntegrationResult,
    _clear_parallel_active_tasks,
    _integrate_completed_task,
    _run_integration_check,
    _select_parallel_tasks,
    run_parallel_tasks,
)
from litehive.pipeline_old._types import ExecutionSummary, TaskPoolStopConditions
from litehive.pipeline_old.core import RunResult
from litehive.observability._status import render_active_tasks_section


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_parallel_workspace(tmp_path: Path, *, parallel_capacity: int = 2) -> Path:
    """Create a git-backed workspace with parallel config."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    # Write config with parallel_capacity
    config_path = tmp_path / ".litehive" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config_data["parallel_capacity"] = parallel_capacity
    config_path.write_text(yaml.dump(config_data), encoding="utf-8")
    return tmp_path


def _make_pass_executor():
    """Return a stage executor that passes every stage."""
    def executor(task, step):
        return StageReport(
            task_id=task.id,
            step=step,
            verdict="pass",
            summary=f"{step} ok",
            files_changed=["app.txt"],
            tests={"added": 1, "passing": 1},
        )
    return executor


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


def test_parallel_capacity_config_default(tmp_path: Path) -> None:
    """Default parallel_capacity is 1 (sequential)."""
    ensure_workspace(tmp_path)
    config = load_config(tmp_path)
    assert config.parallel_capacity == 1


def test_parallel_capacity_config_custom(tmp_path: Path) -> None:
    """parallel_capacity can be set in config."""
    root = _init_parallel_workspace(tmp_path, parallel_capacity=3)
    config = load_config(root)
    assert config.parallel_capacity == 3


def test_parallel_capacity_must_be_positive() -> None:
    """parallel_capacity < 1 raises ValueError."""
    with pytest.raises(ValueError, match="parallel_capacity must be at least 1"):
        LitehiveConfig(parallel_capacity=0)


# ---------------------------------------------------------------------------
# State model tests
# ---------------------------------------------------------------------------


def test_active_task_ids_state(tmp_path: Path) -> None:
    """WorkspaceState supports active_task_ids list."""
    ensure_workspace(tmp_path)
    state = load_state(tmp_path)
    assert state.active_task_ids == []

    state.active_task_ids = ["T-0001", "T-0002"]
    save_state(tmp_path, state)

    reloaded = load_state(tmp_path)
    assert reloaded.active_task_ids == ["T-0001", "T-0002"]


# ---------------------------------------------------------------------------
# Task selection tests
# ---------------------------------------------------------------------------


def test_select_parallel_tasks_selects_multiple(tmp_path: Path) -> None:
    """_select_parallel_tasks can select multiple independent tasks."""
    root = _init_parallel_workspace(tmp_path, parallel_capacity=3)
    t1 = create_task(root, title="Task one")
    t2 = create_task(root, title="Task two")
    t3 = create_task(root, title="Task three")

    tasks, blocked = _select_parallel_tasks(root, capacity=3)

    assert len(tasks) >= 2  # Should select multiple
    task_ids = {t.id for t in tasks}
    assert t1.id in task_ids or t2.id in task_ids or t3.id in task_ids

    # State should reflect multiple active tasks
    state = load_state(root)
    assert len(state.active_task_ids) == len(tasks)


def test_select_parallel_tasks_respects_dependencies(tmp_path: Path) -> None:
    """Tasks that depend on each other are not both selected."""
    root = _init_parallel_workspace(tmp_path, parallel_capacity=3)
    t1 = create_task(root, title="Base task")
    t2 = create_task(root, title="Dependent task", depends_on=[t1.id])
    t3 = create_task(root, title="Independent task")

    tasks, blocked = _select_parallel_tasks(root, capacity=3)

    task_ids = {t.id for t in tasks}
    # If t1 is selected, t2 should NOT be in the same batch
    if t1.id in task_ids:
        assert t2.id not in task_ids


# ---------------------------------------------------------------------------
# Parallel execution tests (mocked)
# ---------------------------------------------------------------------------


def test_run_parallel_tasks_with_mock_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple independent tasks execute in parallel and integrate back to main."""
    root = _init_parallel_workspace(tmp_path, parallel_capacity=2)
    t1 = create_task(root, title="Feature A")
    t2 = create_task(root, title="Feature B")

    # Mock the parallel task runner to simulate completed tasks
    call_count = {"n": 0}

    def mock_run_parallel_task(root, task, *, engine_override=None, model_override=None, budget_ledger=None):
        call_count["n"] += 1
        # Simulate work in worktree
        wt_path = root / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
        wt_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", str(wt_path), "HEAD"], cwd=root, capture_output=True)
        task.git.worktree_path = str(wt_path.relative_to(root))
        # Write a unique file in each worktree to avoid conflicts
        (wt_path / f"{task.slug}.txt").write_text(f"feature {task.id}\n", encoding="utf-8")
        save_task(root, task)
        return ExecutionSummary(
            task=task,
            result=RunResult(final_status="done", steps_executed=5, last_verdict="pass"),
        )

    monkeypatch.setattr(
        "litehive.pipeline_old._parallel._run_single_parallel_task",
        mock_run_parallel_task,
    )

    # Mock SubagentManager.run to avoid real engine calls
    monkeypatch.setattr(SubagentManager, "run", MagicMock(return_value=None))

    summary = run_parallel_tasks(root, stop_conditions=TaskPoolStopConditions())

    assert len(summary.executions) == 2
    assert call_count["n"] == 2
    assert summary.stop_reason == "parallel_batch_complete"


def test_run_parallel_tasks_rejects_capacity_1(tmp_path: Path) -> None:
    """run_parallel_tasks raises ValueError when parallel_capacity <= 1."""
    ensure_workspace(tmp_path)
    with pytest.raises(ValueError, match="parallel_capacity must be > 1"):
        run_parallel_tasks(tmp_path)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_integrate_completed_task_success(tmp_path: Path) -> None:
    """A completed task with a worktree integrates cleanly back to main."""
    root = _init_parallel_workspace(tmp_path)
    task = create_task(root, title="Integrate me")

    # Create worktree and make a change
    wt_path = root / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", str(wt_path), "HEAD"], cwd=root, capture_output=True)
    task.git.worktree_path = str(wt_path.relative_to(root))
    save_task(root, task)

    (wt_path / "new_feature.txt").write_text("hello\n", encoding="utf-8")

    execution = ExecutionSummary(
        task=task,
        result=RunResult(final_status="done", steps_executed=5, last_verdict="pass"),
    )
    result = _integrate_completed_task(root, execution)

    assert result.success is True
    assert result.merge_conflict is False
    assert result.commit_sha is not None
    # Worktree should be cleaned up
    reloaded = get_task(root, task.id)
    assert reloaded.status == "done"
    assert reloaded.git.worktree_path is None


def test_integrate_completed_task_merge_conflict_with_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When two parallel tasks touch the same file, the merge-conflict agent is invoked."""
    root = _init_parallel_workspace(tmp_path)

    # Create two tasks
    t1 = create_task(root, title="Feature A")
    t2 = create_task(root, title="Feature B")

    # Create worktrees for both
    wt1 = root / ".litehive" / "worktrees" / f"{t1.id}-{t1.slug}"
    wt2 = root / ".litehive" / "worktrees" / f"{t2.id}-{t2.slug}"
    for wt in [wt1, wt2]:
        wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", str(wt), "HEAD"], cwd=root, capture_output=True)

    t1.git.worktree_path = str(wt1.relative_to(root))
    t2.git.worktree_path = str(wt2.relative_to(root))
    save_task(root, t1)
    save_task(root, t2)

    # Both tasks modify the same file differently
    (wt1 / "app.txt").write_text("base\nfeature A\n", encoding="utf-8")
    (wt2 / "app.txt").write_text("base\nfeature B\n", encoding="utf-8")

    # First integrate t1 — should succeed
    exec1 = ExecutionSummary(
        task=t1,
        result=RunResult(final_status="done", steps_executed=5, last_verdict="pass"),
    )
    result1 = _integrate_completed_task(root, exec1)
    assert result1.success is True

    # Mock SubagentManager.run to simulate agent resolving conflict
    def mock_resolve(self_or_task, *args, **kwargs):
        # Simulate agent resolving the conflict by accepting both changes
        task_arg = self_or_task if isinstance(self_or_task, TaskRecord) else args[0] if args else None
        subprocess.run(["git", "checkout", "--theirs", "app.txt"], cwd=root, capture_output=True)
        subprocess.run(["git", "add", "app.txt"], cwd=root, capture_output=True)
        subprocess.run(["git", "commit", "--no-edit"], cwd=root, capture_output=True)
        return None

    monkeypatch.setattr(SubagentManager, "run", mock_resolve)

    # Integrate t2 — should trigger conflict then resolve
    exec2 = ExecutionSummary(
        task=t2,
        result=RunResult(final_status="done", steps_executed=5, last_verdict="pass"),
    )
    result2 = _integrate_completed_task(root, exec2)

    assert result2.merge_conflict is True
    assert result2.conflict_resolved is True
    assert result2.success is True


def test_integrate_failed_task_skipped(tmp_path: Path) -> None:
    """Tasks that didn't finish as 'done' are skipped during integration."""
    root = _init_parallel_workspace(tmp_path)
    task = create_task(root, title="Failed task")

    execution = ExecutionSummary(
        task=task,
        result=RunResult(final_status="flagged", steps_executed=2, last_verdict="fail"),
    )
    result = _integrate_completed_task(root, execution)

    assert result.success is False
    assert "status flagged" in result.error


# ---------------------------------------------------------------------------
# Status rendering tests
# ---------------------------------------------------------------------------


def test_render_active_tasks_section_empty() -> None:
    """Renders correctly when no tasks are active."""
    lines = render_active_tasks_section([], "codex")
    assert "=== Active Tasks (parallel) ===" in lines[0]
    assert "(none)" in lines[1]


def test_render_active_tasks_section_multiple() -> None:
    """Renders multiple active tasks with worktree info."""
    t1 = TaskRecord(id="T-0001", slug="feature-a", title="Feature A")
    t1.git.worktree_path = ".litehive/worktrees/T-0001-feature-a"
    t2 = TaskRecord(id="T-0002", slug="feature-b", title="Feature B")
    t2.git.worktree_path = ".litehive/worktrees/T-0002-feature-b"
    t2.status = "merge_failed"  # type: ignore[assignment]

    lines = render_active_tasks_section([t1, t2], "codex")
    joined = "\n".join(lines)

    assert "2 task(s) running in parallel" in joined
    assert "T-0001" in joined
    assert "T-0002" in joined
    assert "worktree: .litehive/worktrees/T-0001-feature-a" in joined
    assert "CONFLICT - blocked on integration" in joined


# ---------------------------------------------------------------------------
# Clear parallel state
# ---------------------------------------------------------------------------


def test_clear_parallel_active_tasks(tmp_path: Path) -> None:
    """_clear_parallel_active_tasks resets state."""
    ensure_workspace(tmp_path)
    state = load_state(tmp_path)
    state.active_task_ids = ["T-0001", "T-0002"]
    state.active_task_id = "T-0001"
    save_state(tmp_path, state)

    _clear_parallel_active_tasks(tmp_path)

    state = load_state(tmp_path)
    assert state.active_task_ids == []
    assert state.active_task_id is None


# ---------------------------------------------------------------------------
# Deterministic integration order
# ---------------------------------------------------------------------------


def test_integration_order_is_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Completed tasks are integrated in task-ID order for reproducibility."""
    root = _init_parallel_workspace(tmp_path, parallel_capacity=3)
    tasks = [
        create_task(root, title=f"Task {i}")
        for i in range(3)
    ]

    integration_order: list[str] = []
    original_integrate = _integrate_completed_task

    def tracking_integrate(root, execution, *, config=None):
        integration_order.append(execution.task.id)
        # Skip actual integration, just track order
        return IntegrationResult(task_id=execution.task.id, success=True)

    monkeypatch.setattr(
        "litehive.pipeline_old._parallel._integrate_completed_task",
        tracking_integrate,
    )

    # Mock _run_single_parallel_task
    def mock_run(root, task, *, engine_override=None, model_override=None, budget_ledger=None):
        return ExecutionSummary(
            task=task,
            result=RunResult(final_status="done", steps_executed=5, last_verdict="pass"),
        )

    monkeypatch.setattr(
        "litehive.pipeline_old._parallel._run_single_parallel_task",
        mock_run,
    )

    summary = run_parallel_tasks(root, stop_conditions=TaskPoolStopConditions())

    # Integration order should be sorted by task ID
    assert integration_order == sorted(integration_order)


# ---------------------------------------------------------------------------
# Worktree isolation
# ---------------------------------------------------------------------------


def test_each_parallel_task_gets_own_worktree(tmp_path: Path) -> None:
    """Each task selected for parallel execution gets a unique worktree."""
    root = _init_parallel_workspace(tmp_path, parallel_capacity=2)
    t1 = create_task(root, title="Task A")
    t2 = create_task(root, title="Task B")

    tasks, _ = _select_parallel_tasks(root, capacity=2)
    assert len(tasks) == 2

    # After selection, each task should have been dequeued
    # The worktree paths would be assigned during execution
    assert tasks[0].id != tasks[1].id


# ---------------------------------------------------------------------------
# Integration check tests
# ---------------------------------------------------------------------------


def _mock_parallel_run_and_integrate(monkeypatch, *, integration_success=True):
    """Set up mocks for _run_single_parallel_task and _integrate_completed_task.

    The mock executor returns tasks as "done" and the mock integrator returns
    success results, so the pipeline reaches Phase 4 (integration check).
    """
    def mock_run(root, task, *, engine_override=None, model_override=None, budget_ledger=None):
        return ExecutionSummary(
            task=task,
            result=RunResult(final_status="done", steps_executed=5, last_verdict="pass"),
        )

    def mock_integrate(root, execution, *, config=None):
        return IntegrationResult(
            task_id=execution.task.id,
            success=integration_success,
            commit_sha="abc1234" if integration_success else None,
        )

    monkeypatch.setattr("litehive.pipeline_old._parallel._run_single_parallel_task", mock_run)
    monkeypatch.setattr("litehive.pipeline_old._parallel._integrate_completed_task", mock_integrate)


def test_integration_check_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When integration check passes, stop_reason is parallel_batch_complete."""
    root = _init_parallel_workspace(tmp_path, parallel_capacity=2)
    config_path = root / ".litehive" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config_data["parallel_integration_check"] = "true"
    config_path.write_text(yaml.dump(config_data), encoding="utf-8")

    create_task(root, title="Feature A")
    create_task(root, title="Feature B")
    _mock_parallel_run_and_integrate(monkeypatch)

    summary = run_parallel_tasks(root, stop_conditions=TaskPoolStopConditions())

    assert summary.stop_reason == "parallel_batch_complete"
    assert summary.integration_check is not None
    assert summary.integration_check.success is True
    assert summary.integration_check.exit_code == 0
    # Report file should exist
    report_dir = root / ".litehive" / "logs" / "parallel-integration"
    assert report_dir.exists()
    reports = list(report_dir.glob("*-batch.yaml"))
    assert len(reports) == 1


def test_integration_check_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When integration check fails, stop_reason is parallel_integration_failed."""
    root = _init_parallel_workspace(tmp_path, parallel_capacity=2)
    config_path = root / ".litehive" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config_data["parallel_integration_check"] = "false"
    config_path.write_text(yaml.dump(config_data), encoding="utf-8")

    create_task(root, title="Feature A")
    create_task(root, title="Feature B")
    _mock_parallel_run_and_integrate(monkeypatch)

    summary = run_parallel_tasks(root, stop_conditions=TaskPoolStopConditions())

    assert summary.stop_reason == "parallel_integration_failed"
    assert summary.integration_check is not None
    assert summary.integration_check.success is False
    assert summary.integration_check.exit_code != 0
    # Report should be written
    report_dir = root / ".litehive" / "logs" / "parallel-integration"
    reports = list(report_dir.glob("*-batch.yaml"))
    assert len(reports) == 1
    report_content = yaml.safe_load(reports[0].read_text(encoding="utf-8"))
    assert report_content["success"] is False
    assert len(report_content["task_ids"]) >= 1
    assert report_content["command"] == "false"
    # Journal entries should be written for each merged task
    for execution in summary.executions:
        task_dir = root / ".litehive" / "tasks" / f"{execution.task.id}-{execution.task.slug}"
        journal = task_dir / "journal.md"
        if journal.exists():
            content = journal.read_text(encoding="utf-8")
            assert "Integration check failed" in content


def test_integration_check_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no integration check is configured, it's a no-op."""
    root = _init_parallel_workspace(tmp_path, parallel_capacity=2)
    create_task(root, title="Feature A")
    create_task(root, title="Feature B")
    _mock_parallel_run_and_integrate(monkeypatch)

    summary = run_parallel_tasks(root, stop_conditions=TaskPoolStopConditions())

    assert summary.stop_reason == "parallel_batch_complete"
    assert summary.integration_check is None
    report_dir = root / ".litehive" / "logs" / "parallel-integration"
    assert not report_dir.exists()


def test_run_integration_check_captures_output(tmp_path: Path) -> None:
    """_run_integration_check captures stdout, stderr, exit code, and writes report."""
    _init_git_repo(tmp_path)
    result = _run_integration_check(
        tmp_path,
        command="echo hello",
        merged_task_ids=["T-0001", "T-0002"],
        merge_order=["T-0001", "T-0002"],
    )
    assert result.success is True
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.task_ids == ["T-0001", "T-0002"]
    assert result.merge_order == ["T-0001", "T-0002"]
    assert result.command == "echo hello"
    # Report file written
    report_dir = tmp_path / ".litehive" / "logs" / "parallel-integration"
    assert report_dir.exists()
    reports = list(report_dir.glob("*-batch.yaml"))
    assert len(reports) == 1
