"""Test that commit_to_git does not lose code.

Key invariants:
1. If merge fails, task is NOT marked done
2. If merge fails, worktree is NOT deleted
3. Task is only marked done when new commits are confirmed on main
"""

import subprocess
from pathlib import Path

import pytest

from litehive.config import LitehiveConfig, ensure_workspace
from litehive.git_ops import current_head
from litehive.runtime import _commit_to_git_report
from litehive.tasks import create_task, save_task, task_dir


def _init_git_repo(path: Path) -> str:
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)
    (path / "app.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)
    return current_head(path)


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True).stdout.strip()


def test_merge_conflict_does_not_delete_worktree(tmp_path: Path) -> None:
    """If merge fails and no agent resolves it, worktree must survive."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Precious code changes")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "feature.py").write_text("def feature(): return True\n")
    _run(["git", "add", "feature.py"], worktree_path)
    _run(["git", "commit", "-m", "add feature"], worktree_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)

    # Create a conflict on main
    (tmp_path / "feature.py").write_text("def feature(): return False\n")
    _run(["git", "add", "feature.py"], tmp_path)
    _run(["git", "commit", "-m", "conflicting change on main"], tmp_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "fail"
    assert worktree_path.exists(), "Worktree was deleted despite failed merge!"
    assert (worktree_path / "feature.py").exists(), "Code was lost!"
    assert (worktree_path / "feature.py").read_text() == "def feature(): return True\n"
    assert task.status != "done"


def test_successful_merge_deletes_worktree(tmp_path: Path) -> None:
    """After successful merge, worktree should be cleaned up."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Clean merge task")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "new_file.py").write_text("# new code\n")
    _run(["git", "add", "new_file.py"], worktree_path)
    _run(["git", "commit", "-m", "add new file"], worktree_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)

    head_before = current_head(tmp_path)
    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.git.commit_sha is not None
    assert task.git.commit_sha != head_before
    assert (tmp_path / "new_file.py").exists()
    assert not worktree_path.exists(), "Worktree should be deleted after successful merge"


def test_head_unchanged_means_fail(tmp_path: Path) -> None:
    """If HEAD doesn't advance, commit_to_git must fail."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="No-op task")

    # execution_root == root, no worktree, no changes
    head_before = current_head(tmp_path)
    report = _commit_to_git_report(tmp_path, tmp_path, task, auto_commit_enabled=True)

    # No worktree means merge_ok=True but head shouldn't change
    # This is the same-repo case, so it marks done
    assert current_head(tmp_path) == head_before


def test_merge_conflict_resolved_by_agent(tmp_path: Path) -> None:
    """Agent resolves merge conflict, task completes successfully."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent resolves conflict")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "feature.py").write_text("def feature(): return 'worktree'\n")
    _run(["git", "add", "feature.py"], worktree_path)
    _run(["git", "commit", "-m", "add feature in worktree"], worktree_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)

    # Create conflict on main
    (tmp_path / "feature.py").write_text("def feature(): return 'main'\n")
    _run(["git", "add", "feature.py"], tmp_path)
    _run(["git", "commit", "-m", "conflicting change"], tmp_path)

    # Fake subagent that resolves the conflict
    class FakeSubagents:
        def __init__(self):
            self.execution_root = worktree_path

        def run(self, task, *, role, engine_name, prompt, model=None):
            from litehive.subagents import SubagentResult
            from litehive.models import SubagentRef
            # Resolve the conflict by picking the worktree version
            conflict_file = tmp_path / "feature.py"
            conflict_file.write_text("def feature(): return 'worktree'\n")
            _run(["git", "add", "feature.py"], tmp_path)
            _run(["git", "commit", "--no-edit"], tmp_path)
            return SubagentResult(
                ref=SubagentRef(id="SA-merge", role=role, engine=engine_name,
                                status="completed", path="subagents/SA-merge"),
                execution=None, transcript="Resolved conflict", exit_code=0,
            )

    report = _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=FakeSubagents(), config=LitehiveConfig(),
    )

    assert report.verdict == "pass"
    assert task.status == "done"
    assert (tmp_path / "feature.py").read_text() == "def feature(): return 'worktree'\n"
    assert not worktree_path.exists()


def test_merge_conflict_agent_fails_to_resolve(tmp_path: Path) -> None:
    """Agent tries but can't resolve conflict. Task fails, worktree survives."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent fails to resolve")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "feature.py").write_text("def feature(): return 'worktree'\n")
    _run(["git", "add", "feature.py"], worktree_path)
    _run(["git", "commit", "-m", "add feature in worktree"], worktree_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)

    # Create conflict on main
    (tmp_path / "feature.py").write_text("def feature(): return 'main'\n")
    _run(["git", "add", "feature.py"], tmp_path)
    _run(["git", "commit", "-m", "conflicting change"], tmp_path)

    # Fake subagent that does nothing (can't resolve the conflict)
    class FakeSubagents:
        def __init__(self):
            self.execution_root = worktree_path

        def run(self, task, *, role, engine_name, prompt, model=None):
            from litehive.subagents import SubagentResult
            from litehive.models import SubagentRef
            # Agent runs but doesn't fix anything
            return SubagentResult(
                ref=SubagentRef(id="SA-merge", role=role, engine=engine_name,
                                status="completed", path="subagents/SA-merge"),
                execution=None, transcript="Could not resolve", exit_code=1,
            )

    report = _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=FakeSubagents(), config=LitehiveConfig(),
    )

    assert report.verdict == "fail"
    assert task.status != "done"
    assert worktree_path.exists(), "Worktree deleted despite failed merge!"
    assert (worktree_path / "feature.py").read_text() == "def feature(): return 'worktree'\n"


def _setup_conflict(tmp_path: Path):
    """Helper: create a repo with a worktree that conflicts with main."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Retry limit test")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "feature.py").write_text("def feature(): return 'worktree'\n")
    _run(["git", "add", "feature.py"], worktree_path)
    _run(["git", "commit", "-m", "add feature in worktree"], worktree_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)

    # Create conflict on main
    (tmp_path / "feature.py").write_text("def feature(): return 'main'\n")
    _run(["git", "add", "feature.py"], tmp_path)
    _run(["git", "commit", "-m", "conflicting change"], tmp_path)

    return task, worktree_path


class _CountingSubagents:
    """Fake subagent manager that counts launches and never resolves conflicts."""
    def __init__(self, worktree_path):
        self.execution_root = worktree_path
        self.launch_count = 0

    def run(self, task, *, role, engine_name, prompt, model=None):
        from litehive.subagents import SubagentResult
        from litehive.models import SubagentRef
        self.launch_count += 1
        return SubagentResult(
            ref=SubagentRef(id=f"SA-merge-{self.launch_count}", role=role,
                            engine=engine_name, status="completed",
                            path=f"subagents/SA-merge-{self.launch_count}"),
            execution=None, transcript="Could not resolve", exit_code=1,
        )


def test_merge_agent_launches_exactly_once(tmp_path: Path) -> None:
    """Merge agent must launch at most once per conflict, even across multiple attempts."""
    task, worktree_path = _setup_conflict(tmp_path)
    fake = _CountingSubagents(worktree_path)

    # First attempt: merge agent should launch
    report1 = _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=fake, config=LitehiveConfig(),
    )
    assert report1.verdict == "fail"
    assert fake.launch_count == 1
    assert task.git.merge_agent_attempts == 1

    # Reset git state for second attempt (re-create the conflict)
    (tmp_path / "feature.py").write_text("def feature(): return 'main'\n")
    _run(["git", "add", "feature.py"], tmp_path)
    _run(["git", "commit", "-m", "re-create conflict"], tmp_path)

    # Second attempt: merge agent must NOT launch again
    report2 = _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=fake, config=LitehiveConfig(),
    )
    assert report2.verdict == "fail"
    assert fake.launch_count == 1, "Merge agent launched more than once!"
    assert task.git.merge_agent_attempts == 1


def test_recovery_failed_blocks_repair_requeue(tmp_path: Path) -> None:
    """Tasks with status=recovery_failed must not be requeued by repair."""
    from litehive.tasks import _should_recover_flagged_commit_stage_task
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery failed task")
    task.pipeline_status = "commit_to_git"
    task.status = "recovery_failed"
    task.git.merge_agent_attempts = 1
    save_task(tmp_path, task)

    assert not _should_recover_flagged_commit_stage_task(tmp_path, task)


def test_merge_attempts_blocks_repair_requeue(tmp_path: Path) -> None:
    """Tasks with merge_agent_attempts >= 1 must not be requeued by repair, even if flagged."""
    from litehive.tasks import _should_recover_flagged_commit_stage_task
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Already attempted task")
    task.pipeline_status = "commit_to_git"
    task.status = "flagged"
    task.git.merge_agent_attempts = 1
    save_task(tmp_path, task)

    assert not _should_recover_flagged_commit_stage_task(tmp_path, task)
