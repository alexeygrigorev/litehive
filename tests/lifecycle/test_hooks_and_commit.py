"""Tests for SubprocessHookRunner and GitCommitNode (real git merge path)."""

import subprocess
from pathlib import Path

import pytest

from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.lifecycle.events import HookOk, MergeConflictDetected, Pass, Reject
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.nodes.hook import HookNode, HookSpec, SubprocessHookRunner
from litehive.lifecycle.nodes.system import GitCommitNode, StubCommitNode
from litehive.lifecycle.orchestration import run_task
from litehive.lifecycle.persistence import SqlitePersistence, TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.state.persist import load_state
from litehive.state.records import create_task, get_task, save_task, set_task_worktree_path
from litehive.tasks.queue import dequeue_next_task
from litehive.tasks.reports import load_task_thread
from litehive.tasks.worktrees import serialize_worktree_path, task_worktree_branch, task_worktree_path


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
    """Create a main repo with a feature worktree branch that can be merged cleanly."""
    repo = tmp_path / "repo"
    worktree = tmp_path / "feature-worktree"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    (repo / "a.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feature", str(worktree), "HEAD"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=worktree, check=True)
    (worktree / "b.txt").write_text("feature\n")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "feature"], cwd=worktree, check=True)

    return repo, worktree


def test_commit_node_clean_merge_returns_pass(git_repo_with_branch) -> None:
    main_repo, worktree = git_repo_with_branch

    node = GitCommitNode(
        main_repo,
        worktree_resolver=lambda state: worktree,
    )

    state = make_state(stage="commit")
    event = node.run(state)
    assert isinstance(event, Pass), event
    assert (main_repo / "b.txt").read_text() == "feature\n"


def test_commit_node_autocommits_uncommitted_worktree_edits(tmp_path: Path) -> None:
    """SWE subagents write files but do not run ``git commit``. GitCommitNode
    must commit those edits before merging, otherwise ``git merge`` sees no
    new commits, reports "Already up to date", and the agent's work is
    silently lost at the terminal stage (empty-pass bug)."""
    repo = tmp_path / "main"
    worktree = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feature", str(worktree), "HEAD"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=worktree, check=True)

    (worktree / "new.txt").write_text("agent wrote this\n")

    node = GitCommitNode(
        repo,
        worktree_resolver=lambda state: worktree,
    )

    state = make_state(stage="commit")
    event = node.run(state)

    assert isinstance(event, Pass), event
    assert (repo / "new.txt").read_text() == "agent wrote this\n"


def test_commit_node_autocommit_excludes_runner_owned_task_metadata(tmp_path: Path) -> None:
    """Runner-owned task metadata must never be captured in the auto-commit.

    Otherwise ``git merge`` aborts with "Your local changes would be
    overwritten" whenever both the worktree and main have edits to the same
    task.yaml (see T-0320).
    """
    repo = tmp_path / "main"
    worktree = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    task_dir = repo / ".litehive" / "tasks" / "T-0001-demo"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("id: T-0001\nstatus: queued\n")
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feature", str(worktree), "HEAD"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=worktree, check=True)

    # Main repo: runner writes an edit to task metadata (simulating the race).
    (repo / ".litehive" / "tasks" / "T-0001-demo" / "task.yaml").write_text(
        "id: T-0001\nstatus: in_progress\n"
    )

    # Worktree: SWE writes real code AND the runner updates task metadata there too.
    (worktree / "new.txt").write_text("agent wrote this\n")
    (worktree / ".litehive" / "tasks" / "T-0001-demo" / "task.yaml").write_text(
        "id: T-0001\nstatus: implementing\n"
    )

    node = GitCommitNode(repo, worktree_resolver=lambda state: worktree)
    event = node.run(make_state(stage="commit", task_id="T-0001"))

    assert isinstance(event, Pass), event
    assert (repo / "new.txt").read_text() == "agent wrote this\n"
    # Main's task.yaml must remain the main-side version — not clobbered by the merge.
    assert (repo / ".litehive" / "tasks" / "T-0001-demo" / "task.yaml").read_text() == (
        "id: T-0001\nstatus: in_progress\n"
    )


def test_commit_node_reports_already_landed_noop_reconciliation(git_repo_with_branch, monkeypatch) -> None:
    main_repo, worktree = git_repo_with_branch

    node = GitCommitNode(
        main_repo,
        worktree_resolver=lambda state: worktree,
    )

    monkeypatch.setattr(node, "_autocommit_worktree_changes", lambda worktree, state: None)
    monkeypatch.setattr(node, "_worktree_branch", lambda worktree: "feature")
    monkeypatch.setattr(node, "_worktree_head", lambda worktree: "feature-head")
    monkeypatch.setattr(node, "_main_head", lambda: "main-head")
    monkeypatch.setattr(
        node,
        "_git_merge",
        lambda branch_ref: subprocess.CompletedProcess(
            args=["git", "merge", branch_ref, "--no-edit"],
            returncode=0,
            stdout="Already up to date.\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(node, "_worktree_patch_already_on_main", lambda wt_head, main_head: True)

    event = node.run(make_state(stage="commit"))

    assert isinstance(event, Pass), event
    assert event.metadata == {
        "commit_result": {
            "status": "reconciled_noop",
            "reason": "already_landed",
            "head_sha": "main-head",
        }
    }


def test_commit_node_with_conflict_emits_merge_conflict_detected(
    git_repo_with_branch,
) -> None:
    """GitCommitNode no longer delegates to the merge agent — it emits
    ``MergeConflictDetected`` and the state machine routes the task to
    the ``merge_resolving`` node on the next step."""
    main_repo, worktree = git_repo_with_branch

    # Create a conflict: modify a.txt on both feature and main
    (worktree / "a.txt").write_text("feature_change\n")
    subprocess.run(["git", "commit", "-qam", "feature change"], cwd=worktree, check=True)
    (main_repo / "a.txt").write_text("main_change\n")
    subprocess.run(["git", "commit", "-qam", "main change"], cwd=main_repo, check=True)

    node = GitCommitNode(
        main_repo,
        worktree_resolver=lambda state: worktree,
    )

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


def test_commit_node_concludes_resolved_in_progress_merge(git_repo_with_branch) -> None:
    main_repo, worktree = git_repo_with_branch

    (worktree / "a.txt").write_text("feature_change\n")
    subprocess.run(["git", "commit", "-qam", "feature change"], cwd=worktree, check=True)
    (main_repo / "a.txt").write_text("main_change\n")
    subprocess.run(["git", "commit", "-qam", "main change"], cwd=main_repo, check=True)

    node = GitCommitNode(
        main_repo,
        worktree_resolver=lambda state: worktree,
    )

    conflict_event = node.run(make_state(stage="commit"))
    assert isinstance(conflict_event, MergeConflictDetected)

    (main_repo / "a.txt").write_text("main_change\nfeature_change\n")
    subprocess.run(["git", "add", "a.txt"], cwd=main_repo, check=True)

    event = node.run(make_state(stage="commit"))

    assert isinstance(event, Pass), event
    assert (main_repo / "a.txt").read_text() == "main_change\nfeature_change\n"
    merge_head = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=main_repo,
        capture_output=True,
        text=True,
    )
    assert merge_head.returncode != 0


# ── StubCommitNode still works (sanity) ─────────────────────────────────


def test_stub_commit_node_always_passes() -> None:
    node = StubCommitNode()
    event = node.run(make_state(stage="commit"))
    assert isinstance(event, Pass)


class _AlwaysPassEngine:
    def __init__(self, name: str) -> None:
        self.name = name

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        return AgentVerdict(outcome="pass")


def _init_workspace_git_repo(root: Path, *, config: LitehiveConfig | None = None) -> None:
    ensure_workspace(root, config)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def _prepare_committed_task_worktree(root: Path, task, *, filename: str = "merged.txt") -> Path:
    worktree = task_worktree_path(root, task)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", task_worktree_branch(task), str(worktree), "HEAD"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=worktree, check=True)
    (worktree / filename).write_text("merged\n")
    subprocess.run(["git", "add", filename], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "feature"], cwd=worktree, check=True)
    set_task_worktree_path(task, serialize_worktree_path(worktree))
    save_task(root, task)
    return worktree


def test_run_task_runs_after_merge_hook_on_main_and_finishes(tmp_path: Path) -> None:
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hooks={
                "after_merge": [
                    {
                        "command": "git branch --show-current > after_merge_branch.txt && test -f merged.txt",
                        "blocking": True,
                    }
                ]
            }
        ),
    )
    create_task(tmp_path, title="After merge pass")
    task = dequeue_next_task(tmp_path)
    assert task is not None
    worktree = _prepare_committed_task_worktree(tmp_path, task)

    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _AlwaysPassEngine(engine_name),
    )
    refreshed = get_task(tmp_path, task.id)

    assert result.final_stage == "done"
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert (tmp_path / "merged.txt").read_text() == "merged\n"
    assert (tmp_path / "after_merge_branch.txt").read_text().strip() == "main"
    assert not worktree.exists()


def test_run_task_requeues_implementing_when_after_merge_hook_fails(tmp_path: Path) -> None:
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hooks={
                "after_merge": [
                    {
                        "command": "git branch --show-current > after_merge_branch.txt && echo fail && exit 1",
                        "blocking": True,
                    }
                ]
            }
        ),
    )
    create_task(tmp_path, title="After merge fail")
    task = dequeue_next_task(tmp_path)
    assert task is not None
    worktree = _prepare_committed_task_worktree(tmp_path, task)

    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _AlwaysPassEngine(engine_name),
    )
    refreshed = get_task(tmp_path, task.id)
    state = load_state(tmp_path)
    pipeline_state = SqlitePersistence(tmp_path).load(task.id)

    assert result.final_stage == "implementing"
    assert result.failed_reason == "after_merge_hook_failed"
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.git.commit_sha is None
    assert refreshed.runtime.git.worktree_path is None
    assert (tmp_path / "merged.txt").read_text() == "merged\n"
    assert (tmp_path / "after_merge_branch.txt").read_text().strip() == "main"
    assert not worktree.exists()
    assert state.active_task_id is None
    assert state.queue[0] == task.id
    assert pipeline_state.stage == "implementing"
    assert pipeline_state.last_rejection_by_stage["implementing"].source == "hook"
    assert pipeline_state.last_rejection_by_stage["implementing"].raised_at_phase == "after_merge"
    assert "After-merge verification failed on `main`" in (
        pipeline_state.last_rejection_by_stage["implementing"].reason
    )
    assert "echo fail && exit 1" in pipeline_state.last_rejection_by_stage["implementing"].reason

    thread = load_task_thread(tmp_path, refreshed)
    assert thread[-1].role == "hook"
    assert thread[-1].stage == "implementing"
    assert thread[-1].verdict == "reject"
    assert "fix the merged state" in thread[-1].message


def test_run_task_skips_after_merge_when_hook_not_configured(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path, config=LitehiveConfig())
    create_task(tmp_path, title="After merge skipped")
    task = dequeue_next_task(tmp_path)
    assert task is not None
    worktree = _prepare_committed_task_worktree(tmp_path, task)

    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _AlwaysPassEngine(engine_name),
    )
    refreshed = get_task(tmp_path, task.id)

    assert result.final_stage == "done"
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert (tmp_path / "merged.txt").read_text() == "merged\n"
    assert not (tmp_path / "after_merge_branch.txt").exists()
    assert not worktree.exists()
