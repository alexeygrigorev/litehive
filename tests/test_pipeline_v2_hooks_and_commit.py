"""Tests for SubprocessHookRunner and GitCommitNode (real git merge path)."""

import subprocess
from pathlib import Path

import pytest

from litehive.pipeline.events import Crash, HookOk, MergeConflictDetected, Pass, Reject
from litehive.pipeline.nodes import (
    GitCommitNode,
    HookNode,
    HookSpec,
    StubCommitNode,
    SubprocessHookRunner,
)
from litehive.pipeline.persistence import TaskState
from litehive.pipeline.types import PipelineMode


def make_state(stage: str = "before_grooming", task_id: str = "T-0001") -> TaskState:
    return TaskState(task_id=task_id, stage=stage, pipeline_mode=PipelineMode.FULL)


# ── SubprocessHookRunner ────────────────────────────────────────────────


def test_hook_passing_command_returns_ok(tmp_path: Path) -> None:
    runner = SubprocessHookRunner(tmp_path)
    result = runner.run(HookSpec(command="true"), make_state())
    assert result.ok is True


def test_hook_failing_command_returns_not_ok_with_output(tmp_path: Path) -> None:
    runner = SubprocessHookRunner(tmp_path)
    result = runner.run(HookSpec(command="echo nope && exit 2"), make_state())
    assert result.ok is False
    assert "nope" in result.output


def test_hook_timeout_is_reported_as_not_ok(tmp_path: Path) -> None:
    runner = SubprocessHookRunner(tmp_path)
    result = runner.run(HookSpec(command="sleep 5", timeout_seconds=1), make_state())
    assert result.ok is False
    assert "timeout" in result.output.lower()


def test_hook_environment_contains_task_id_and_stage(tmp_path: Path) -> None:
    runner = SubprocessHookRunner(tmp_path)
    result = runner.run(
        HookSpec(command='echo "$LITEHIVE_TASK_ID:$LITEHIVE_STAGE"'),
        make_state(stage="grooming", task_id="T-0042"),
    )
    assert result.ok is True
    assert "T-0042:grooming" in result.output


def test_hook_node_with_passing_spec_emits_hook_ok(tmp_path: Path) -> None:
    node = HookNode(
        "before_grooming",
        hooks=[HookSpec(command="true")],
        runner=SubprocessHookRunner(tmp_path),
    )
    event = node.run(make_state())
    assert isinstance(event, HookOk)


def test_hook_node_with_failing_spec_emits_reject(tmp_path: Path) -> None:
    node = HookNode(
        "before_grooming",
        hooks=[HookSpec(command="false")],
        runner=SubprocessHookRunner(tmp_path),
    )
    event = node.run(make_state())
    assert isinstance(event, Reject)
    assert event.source == "hook"


# ── GitCommitNode ───────────────────────────────────────────────────────


@pytest.fixture
def git_repo_with_branch(tmp_path: Path) -> tuple[Path, Path]:
    """Create a main repo with a feature branch that can be merged cleanly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    (repo / "a.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
    (repo / "b.txt").write_text("feature\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "feature"], cwd=repo, check=True)

    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    return repo, repo  # main repo and "worktree" point to same path for simplicity


def _resolve_same_repo(repo: Path):
    def _resolver(state):
        return repo

    return _resolver


def test_commit_node_clean_merge_returns_pass(git_repo_with_branch) -> None:
    main_repo, _ = git_repo_with_branch

    node = GitCommitNode(
        main_repo,
        worktree_resolver=lambda state: main_repo,
    )
    # Point the merge at 'feature' — simple override so we don't need a
    # separate worktree path for this smoke test
    node._worktree_head = lambda wt: "feature"

    state = make_state(stage="commit")
    event = node.run(state)
    assert isinstance(event, Pass), event


def test_commit_node_with_conflict_emits_merge_conflict_detected(
    git_repo_with_branch,
) -> None:
    """GitCommitNode no longer delegates to the merge agent — it emits
    ``MergeConflictDetected`` and the state machine routes the task to
    the ``merge_resolving`` node on the next step."""
    main_repo, _ = git_repo_with_branch

    # Create a conflict: modify a.txt on both feature and main
    subprocess.run(["git", "checkout", "-q", "feature"], cwd=main_repo, check=True)
    (main_repo / "a.txt").write_text("feature_change\n")
    subprocess.run(["git", "commit", "-qam", "feature change"], cwd=main_repo, check=True)

    subprocess.run(["git", "checkout", "-q", "main"], cwd=main_repo, check=True)
    (main_repo / "a.txt").write_text("main_change\n")
    subprocess.run(["git", "commit", "-qam", "main change"], cwd=main_repo, check=True)

    node = GitCommitNode(
        main_repo,
        worktree_resolver=lambda state: main_repo,
    )
    node._worktree_head = lambda wt: "feature"

    state = make_state(stage="commit")
    event = node.run(state)
    assert isinstance(event, MergeConflictDetected)
    assert "a.txt" in event.conflict_files

    # Leaves the worktree in the unresolved state so the merge agent can
    # still see the conflict markers
    unresolved = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=main_repo,
        capture_output=True,
        text=True,
    )
    assert "a.txt" in unresolved.stdout


# ── StubCommitNode still works (sanity) ─────────────────────────────────


def test_stub_commit_node_always_passes() -> None:
    node = StubCommitNode()
    event = node.run(make_state(stage="commit"))
    assert isinstance(event, Pass)
