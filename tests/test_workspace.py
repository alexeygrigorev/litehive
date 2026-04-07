"""Focused commit_to_git reconciliation tests for T-0067.

These tests prove that commit_to_git reruns and stale-runner recovery
reconcile an existing checkpoint commit without creating a duplicate,
and that checkpoint_attempts and git history do not advance during
reconciliation.
"""
from tests.workspace_helpers import *  # noqa: F401,F403


def test_commit_to_git_rerun_reconciles_existing_checkpoint(tmp_path: Path) -> None:
    """Rerunning commit_to_git when the checkpoint commit already exists
    must record the existing SHA, mark the task done, and not create a
    second checkpoint commit."""
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Rerun checkpoint reconciliation")
    (tmp_path / "app.txt").write_text("checkpointed\n", encoding="utf-8")

    task.git.checkpoint_attempts = 1
    task.git.checkpoint_base_sha = initial_sha
    save_task(tmp_path, task)

    commit_msg = checkpoint_message(task, attempt=1)
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_msg], tmp_path)
    existing_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    commit_count_before = _run(["git", "rev-list", "--count", "HEAD"], tmp_path)

    report = _commit_to_git_report(tmp_path, tmp_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None
    # No duplicate commit was created
    assert _run(["git", "rev-list", "--count", "HEAD"], tmp_path) == commit_count_before
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_msg


def test_recovery_finalizes_stranded_commit_to_git_with_existing_checkpoint(
    tmp_path: Path,
) -> None:
    """Stale-runner recovery must reconcile an existing checkpoint commit
    for a stranded commit_to_git task before queuing new work, without
    incrementing checkpoint_attempts or advancing git history."""
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Stranded commit")
    follow_up = create_task(tmp_path, title="Follow-up task", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    commit_msg = checkpoint_message(stranded, attempt=1)
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_msg], tmp_path)
    existing_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    commit_count_before = _run(["git", "rev-list", "--count", "HEAD"], tmp_path)

    stranded.status = "in_progress"
    stranded.pipeline_status = "commit_to_git"
    stranded.git.checkpoint_attempts = 1
    stranded.git.checkpoint_base_sha = initial_sha
    stranded.git.commit_sha = None
    stranded.runtime.execution_status = "running"
    stranded.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, stranded)
    save_task_runtime(tmp_path, stranded)

    state = load_state(tmp_path)
    state.active_task_id = stranded.id
    state.queue = [follow_up.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == follow_up.id
    refreshed = require_task(tmp_path, stranded.id)
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == existing_sha
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.runtime.execution_status == "done"
    # Git history was not advanced
    assert _run(["git", "rev-list", "--count", "HEAD"], tmp_path) == commit_count_before
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_msg
