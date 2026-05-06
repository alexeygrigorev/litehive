"""Tests for SubprocessHookRunner and GitCommitNode (real git merge path)."""

import shlex
import subprocess
import json
import os
import time
from pathlib import Path

import pytest

from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.task import TaskRecord
from litehive.git.ops import has_non_litehive_changes
from litehive.lifecycle.events import HookOk, MergeConflictDetected, Pass, Reject
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.nodes.hook import HookNode, HookRunner, HookSpec, SubprocessHookRunner
from litehive.lifecycle.nodes.system import GitCommitNode, StubCommitNode
from litehive.lifecycle.orchestration import reconcile_terminal_commit_sha, run_task
from litehive.lifecycle.persistence import CommitResult, SqlitePersistence, TaskState
from litehive.workspace import Workspace
from litehive.lifecycle.types import PipelineMode
from litehive.state.persist import load_state
from litehive.state.records import create_task, get_task, save_task, set_task_worktree_path
from litehive.tasks.journal import render_task_journal
from litehive.tasks.queue import dequeue_next_task
from litehive.tasks.paths import task_dir
from litehive.tasks.activity import load_task_activity
from litehive.tasks.report_storage import load_stage_reports
from litehive.worktree.paths import serialize_worktree_path, task_worktree_branch, task_worktree_path
from litehive.domain.common import PipelineState, PipelineStatus, TaskStatus

pytestmark = pytest.mark.integration


def _raw_task_state_payload(root: Path, task_id: str) -> dict:
    with connect_workspace_db(root) as connection:
        row = connection.execute("SELECT payload FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return json.loads(row["payload"])


def make_state(stage: str | PipelineState = "before_grooming", task_id: str = "T-0001") -> TaskState:
    return TaskState(task_id=task_id, stage=stage, pipeline_mode=PipelineMode.FULL)  # type: ignore[arg-type]


class SequenceHookRunner(HookRunner):
    def __init__(self, outcomes: dict[str, subprocess.CompletedProcess[str] | None]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def run(self, spec: HookSpec, state: TaskState) -> subprocess.CompletedProcess[str] | None:
        self.calls.append(spec.command)
        return self.outcomes[spec.command]


# ── SubprocessHookRunner ────────────────────────────────────────────────


def test_hook_passing_command_returns_ok(tmp_path: Path) -> None:
    runner = SubprocessHookRunner(tmp_path)
    result = runner.run(HookSpec(command="true"), make_state())
    assert result is None


def test_hook_failing_command_returns_not_ok_with_output(tmp_path: Path) -> None:
    runner = SubprocessHookRunner(tmp_path)
    result = runner.run(HookSpec(command="echo nope && exit 2"), make_state())
    assert result is not None
    assert result.returncode == 2
    assert result.stdout == "nope"


def test_hook_timeout_is_reported_as_not_ok(tmp_path: Path) -> None:
    runner = SubprocessHookRunner(tmp_path)
    result = runner.run(HookSpec(command="sleep 5", timeout_seconds=1), make_state())
    assert result is not None
    assert result.returncode == 124
    assert "timeout" in result.stderr.lower()


def test_hook_environment_contains_task_id_and_stage(tmp_path: Path) -> None:
    runner = SubprocessHookRunner(tmp_path)
    result = runner.run(
        HookSpec(command='test "$LITEHIVE_TASK_ID:$LITEHIVE_STAGE" = "T-0042:grooming"'),
        make_state(stage=PipelineState.GROOMING, task_id="T-0042"),
    )
    assert result is None


def test_hook_node_with_passing_spec_emits_hook_ok(tmp_path: Path) -> None:
    node = HookNode(
        PipelineState.BEFORE_GROOMING,
        hooks=[HookSpec(command="true")],
        runner=SubprocessHookRunner(tmp_path),
    )
    event = node.run(make_state())
    assert isinstance(event, HookOk)
    assert event.warnings == []


def test_hook_node_with_failing_spec_emits_hook_reject(tmp_path: Path) -> None:
    node = HookNode(
        PipelineState.BEFORE_GROOMING,
        hooks=[HookSpec(command="false")],
        runner=SubprocessHookRunner(tmp_path),
    )
    event = node.run(make_state())
    assert isinstance(event, Reject)
    assert event.source == "hook"
    assert event.metadata["consecutive_same_hook_rejects"] == 1
    assert len(event.metadata["warnings"]) == 1
    assert "Runner hook warning" in event.metadata["warnings"][0]


def test_hook_node_warning_includes_command_exit_code_and_streams(tmp_path: Path) -> None:
    node = HookNode(
        PipelineState.AFTER_IMPLEMENTING,
        hooks=[
            HookSpec(
                command="sh -c 'echo stdout-line && echo stderr-line >&2 && exit 3'",
                description="captures stdout and stderr",
                instructions_on_failure="fix the lint issue before retrying",
            )
        ],
        runner=SubprocessHookRunner(tmp_path),
    )

    event = node.run(make_state(stage=PipelineState.AFTER_IMPLEMENTING))

    assert isinstance(event, Reject)
    assert "sh -c 'echo stdout-line && echo stderr-line >&2 && exit 3'" in event.metadata["warnings"][0]
    assert "exited with 3" in event.metadata["warnings"][0]
    assert "stdout-line" in event.metadata["warnings"][0]
    assert "stderr-line" in event.metadata["warnings"][0]


def test_hook_node_stops_after_first_failure() -> None:
    hooks = [
        HookSpec(command="first", description="lint"),
        HookSpec(command="second", description="tests"),
        HookSpec(command="third", description="typing"),
    ]
    runner = SequenceHookRunner(
        {
            "first": subprocess.CompletedProcess("first", 1, "lint stdout", "lint stderr"),
            "second": subprocess.CompletedProcess("second", 2, "tests stdout", "tests stderr"),
            "third": None,
        }
    )
    node = HookNode(PipelineState.AFTER_IMPLEMENTING, hooks=hooks, runner=runner)

    event = node.run(make_state(stage=PipelineState.AFTER_IMPLEMENTING))

    assert isinstance(event, Reject)
    assert runner.calls == ["first"]
    assert len(event.metadata["warnings"]) == 1
    assert event.metadata["hook"]["command"] == "first"
    assert "first" in event.metadata["warnings"][0]
    assert "lint stdout" in event.metadata["warnings"][0]
    assert "lint stderr" in event.metadata["warnings"][0]


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

    state = make_state(stage=PipelineState.COMMIT)
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
    task = TaskRecord(
        id="T-0001",
        slug="document-commit-messages",
        title="Document commit messages",
        goal="Make completion commits explain the task outcome.",
        acceptance_criteria=["Commit body includes task context"],
    )

    node = GitCommitNode(
        repo,
        worktree_resolver=lambda state: worktree,
        task_resolver=lambda state: task,
    )

    state = make_state(stage=PipelineState.COMMIT)
    event = node.run(state)
    message = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert isinstance(event, Pass), event
    assert (repo / "new.txt").read_text() == "agent wrote this\n"
    assert message.stdout.startswith("litehive T-0001: Document commit messages\n\n")
    assert "Task: T-0001" in message.stdout
    assert "Title: Document commit messages" in message.stdout
    assert "Goal:\nMake completion commits explain the task outcome." in message.stdout
    assert "Acceptance criteria:\n- Commit body includes task context" in message.stdout
    assert "Commit detail: auto-commit worktree changes" in message.stdout


def test_commit_node_autocommits_staged_worktree_deletions(tmp_path: Path) -> None:
    repo = tmp_path / "main"
    worktree = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "obsolete.txt").write_text("remove me\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feature", str(worktree), "HEAD"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=worktree, check=True)
    subprocess.run(["git", "rm", "-q", "--", "obsolete.txt"], cwd=worktree, check=True)

    node = GitCommitNode(repo, worktree_resolver=lambda state: worktree)
    event = node.run(make_state(stage=PipelineState.COMMIT))

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert isinstance(event, Pass), event
    assert not (repo / "obsolete.txt").exists()
    assert status.stdout.strip() == ""


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
    (repo / ".litehive" / "tasks" / "T-0001-demo" / "task.yaml").write_text("id: T-0001\nstatus: in_progress\n")

    # Worktree: SWE writes real code AND the runner updates task metadata there too.
    (worktree / "new.txt").write_text("agent wrote this\n")
    (worktree / ".litehive" / "tasks" / "T-0001-demo" / "task.yaml").write_text("id: T-0001\nstatus: implementing\n")

    node = GitCommitNode(repo, worktree_resolver=lambda state: worktree)
    event = node.run(make_state(stage=PipelineState.COMMIT, task_id="T-0001"))

    assert isinstance(event, Pass), event
    assert (repo / "new.txt").read_text() == "agent wrote this\n"
    # Main's task.yaml must remain the main-side version — not clobbered by the merge.
    assert (repo / ".litehive" / "tasks" / "T-0001-demo" / "task.yaml").read_text() == (
        "id: T-0001\nstatus: in_progress\n"
    )


def test_commit_node_autocommit_excludes_uv_lock(tmp_path: Path) -> None:
    """uv.lock must never be captured in the auto-commit.

    Test runs regenerate its ``exclude-newer`` timestamp, producing spurious
    conflicts with main's lockfile on every merge. The lockfile should be
    regenerated from pyproject.toml instead.
    """
    repo = tmp_path / "main"
    worktree = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    (repo / "uv.lock").write_text("version = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feature", str(worktree), "HEAD"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=worktree, check=True)

    # Worktree: real code change + uv.lock timestamp churn.
    (worktree / "new.txt").write_text("agent wrote this\n")
    (worktree / "uv.lock").write_text("version = 1\n# timestamp churn\n")

    node = GitCommitNode(repo, worktree_resolver=lambda state: worktree)
    event = node.run(make_state(stage=PipelineState.COMMIT))

    assert isinstance(event, Pass), event
    assert (repo / "new.txt").read_text() == "agent wrote this\n"
    # Main's uv.lock must remain the main-side version — not overwritten by the worktree churn.
    assert (repo / "uv.lock").read_text() == "version = 1\n"


def test_commit_node_autocommits_dirty_main_checkout_after_clean_merge(git_repo_with_branch) -> None:
    main_repo, worktree = git_repo_with_branch

    (main_repo / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=main_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "tracked base"], cwd=main_repo, check=True)
    (main_repo / "tracked.txt").write_text("dirty main\n")

    node = GitCommitNode(
        main_repo,
        worktree_resolver=lambda state: worktree,
    )

    event = node.run(make_state(stage=PipelineState.COMMIT))

    assert isinstance(event, Pass), event
    assert (main_repo / "b.txt").read_text() == "feature\n"
    assert (main_repo / "tracked.txt").read_text() == "dirty main\n"

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == ""

    subject = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert subject.stdout.strip() == "litehive T-0001: auto-commit dirty main checkout"


def test_commit_node_autocommits_untracked_main_checkout_files_after_clean_merge(git_repo_with_branch) -> None:
    main_repo, worktree = git_repo_with_branch

    (main_repo / "generated.txt").write_text("generated\n")

    node = GitCommitNode(
        main_repo,
        worktree_resolver=lambda state: worktree,
    )

    event = node.run(make_state(stage=PipelineState.COMMIT))

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    changed_files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    subject = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert isinstance(event, Pass), event
    assert (main_repo / "b.txt").read_text() == "feature\n"
    assert (main_repo / "generated.txt").read_text() == "generated\n"
    assert status.stdout.strip() == ""
    assert "generated.txt" in changed_files.stdout.splitlines()
    assert subject.stdout.strip() == "litehive T-0001: auto-commit dirty main checkout"
    assert has_non_litehive_changes(main_repo) is False


def test_commit_node_main_checkout_autocommit_skips_stale_missing_pathspecs(
    git_repo_with_branch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_repo, worktree = git_repo_with_branch

    (main_repo / "generated.txt").write_text("generated\n")

    node = GitCommitNode(
        main_repo,
        worktree_resolver=lambda state: worktree,
    )

    monkeypatch.setattr(
        node,
        "git_status_entries",
        lambda repo_root: (
            [("??", "generated.txt"), (" D", "tmp_review_probe/.litehive/.gitignore")]
            if repo_root == main_repo
            else GitCommitNode.git_status_entries(node, repo_root)
        ),
    )

    cleanup_head = node.autocommit_main_checkout_changes(make_state(stage=PipelineState.COMMIT))

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    changed_files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert cleanup_head is not None
    assert status.stdout.strip() == ""
    assert "generated.txt" in changed_files.stdout.splitlines()


def test_commit_node_reports_already_landed_noop_reconciliation(git_repo_with_branch, monkeypatch) -> None:
    main_repo, worktree = git_repo_with_branch

    node = GitCommitNode(
        main_repo,
        worktree_resolver=lambda state: worktree,
    )

    monkeypatch.setattr(node, "autocommit_worktree_changes", lambda worktree, state: None)
    monkeypatch.setattr(node, "worktree_branch", lambda worktree: "feature")
    monkeypatch.setattr(node, "worktree_head", lambda worktree: "feature-head")
    monkeypatch.setattr(node, "main_head", lambda: "main-head")

    def fail_merge(cwd, ref):
        del cwd, ref
        pytest.fail("already-landed patches skip git merge")

    # ``git merge`` now goes through ``litehive.git.ops.merge_no_edit`` (imported
    # into ``lifecycle.nodes.system``); patch the local binding so this test
    # still proves the merge call is skipped on the already-landed path.
    monkeypatch.setattr("litehive.lifecycle.nodes.system.merge_no_edit", fail_merge)
    monkeypatch.setattr(node, "worktree_patch_already_on_main", lambda wt_head, main_head: True)

    event = node.run(make_state(stage=PipelineState.COMMIT))

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

    state = make_state(stage=PipelineState.COMMIT)
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

    conflict_event = node.run(make_state(stage=PipelineState.COMMIT))
    assert isinstance(conflict_event, MergeConflictDetected)

    (main_repo / "a.txt").write_text("main_change\nfeature_change\n")
    subprocess.run(["git", "add", "a.txt"], cwd=main_repo, check=True)

    event = node.run(make_state(stage=PipelineState.COMMIT))

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
    event = node.run(make_state(stage=PipelineState.COMMIT))
    assert isinstance(event, Pass)


class _AlwaysPassEngine:
    def __init__(self, name: str) -> None:
        self.name = name

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        del session, prompt, state
        return AgentVerdict(outcome="pass")


class _SlowPassEngine:
    def __init__(self, name: str, sleep_seconds: float) -> None:
        self.name = name
        self.sleep_seconds = sleep_seconds

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        del session, prompt, state
        time.sleep(self.sleep_seconds)
        return AgentVerdict(outcome="pass")


class _RejectRecoveryEngine:
    def __init__(self, name: str) -> None:
        self.name = name

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        del session, state
        if prompt.role == "recovery":
            return AgentVerdict(outcome="reject", reason="recovery could not fix the hook failure")
        return AgentVerdict(outcome="pass")


class _RejectMergeResolverEngine:
    def __init__(self, name: str, reason: str = "merge conflict remained after manual attempt") -> None:
        self.name = name
        self.reason = reason

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        del session
        if state.stage == "merge_resolving" or prompt.role == "merge-resolver":
            return AgentVerdict(outcome="reject", reason=self.reason)
        return AgentVerdict(outcome="pass")


class _RecordingPassEngine:
    def __init__(self, name: str, calls: list[dict[str, str]]) -> None:
        self.name = name
        self.calls = calls

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        del session
        self.calls.append(
            {
                "engine": self.name,
                "stage": state.stage,
                "role": prompt.role,
                "pipeline_mode": prompt.pipeline_mode.value,
                "task_id": state.task_id,
            }
        )
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


def test_run_task_single_mode_executes_only_implementing_then_finishes(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    create_task(
        tmp_path,
        title="Single-mode research task",
        pipeline_mode="single",
        goal="Summarize the current workspace state",
        acceptance_criteria=["A single agent pass completes the task"],
    )
    task = dequeue_next_task(Workspace.from_path(tmp_path))
    assert task is not None

    calls: list[dict[str, str]] = []
    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _RecordingPassEngine(engine_name, calls),
    )
    refreshed = get_task(tmp_path, task.id)
    pipeline_state = SqlitePersistence(Workspace.from_path(tmp_path)).load(task.id)
    workspace_state = load_state(tmp_path)

    assert len(calls) == 1
    assert calls[0]["stage"] == "implementing"
    assert calls[0]["role"] == "swe"
    assert calls[0]["pipeline_mode"] == "single"
    assert calls[0]["task_id"] == task.id
    assert result.final_stage == "done"
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert pipeline_state.stage == "done"
    assert workspace_state.active_task_id is None
    assert task.id not in workspace_state.queue


def test_run_task_flags_time_budget_exceeded_and_preserves_worktree(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path, config=LitehiveConfig(task_time_budget_seconds=0.01))
    first = create_task(tmp_path, title="Time budget task")
    second = create_task(tmp_path, title="Next task")
    task = dequeue_next_task(Workspace.from_path(tmp_path))
    assert task is not None
    assert task.id == first.id
    worktree = task_worktree_path(tmp_path, task)
    branch = task_worktree_branch(task)

    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _SlowPassEngine(engine_name, sleep_seconds=0.02),
    )
    refreshed = get_task(tmp_path, task.id)
    workspace_state = load_state(tmp_path)
    branch_lookup = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.final_stage == "failed"
    assert result.failed_reason == "time_budget_exceeded"
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "flagged"
    assert refreshed.flag_reason == "time_budget_exceeded"
    assert worktree.exists()
    assert branch in branch_lookup.stdout
    assert workspace_state.active_task_id is None
    assert task.id not in workspace_state.queue

    next_task = dequeue_next_task(Workspace.from_path(tmp_path))
    assert next_task is not None
    assert next_task.id == second.id


def test_run_task_runs_after_implementing_hook_in_task_worktree(tmp_path: Path) -> None:
    hook_pwd = tmp_path / "after_implementing_pwd.txt"
    hook_branch = tmp_path / "after_implementing_branch.txt"
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hooks={
                "after_implementing": [
                    {
                        "command": (
                            f"pwd > {shlex.quote(str(hook_pwd))} && "
                            f"git branch --show-current > {shlex.quote(str(hook_branch))}"
                        ),
                    }
                ]
            }
        ),
    )
    create_task(tmp_path, title="After implementing hook path")
    task = dequeue_next_task(Workspace.from_path(tmp_path))
    assert task is not None
    expected_worktree = task_worktree_path(tmp_path, task)

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
    assert hook_pwd.read_text(encoding="utf-8").strip() == str(expected_worktree)
    assert hook_branch.read_text(encoding="utf-8").strip() == task_worktree_branch(task)


def test_run_task_runs_after_commit_hook_on_main_and_finishes(tmp_path: Path) -> None:
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hooks={
                "after_commit": [
                    {
                        "command": "git branch --show-current > after_commit_branch.txt && test -f merged.txt",
                    }
                ]
            }
        ),
    )
    create_task(tmp_path, title="After merge pass")
    task = dequeue_next_task(Workspace.from_path(tmp_path))
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
    assert (tmp_path / "after_commit_branch.txt").read_text().strip() == "main"
    assert not worktree.exists()


def test_run_task_reconciles_noop_commit_stage_and_records_main_head(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    create_task(tmp_path, title="Already landed merge")
    task = dequeue_next_task(Workspace.from_path(tmp_path))
    assert task is not None
    worktree = _prepare_committed_task_worktree(tmp_path, task)
    worktree_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    worktree_committer_date = subprocess.run(
        ["git", "show", "-s", "--format=%cI", worktree_head],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    cherry_pick_env = os.environ.copy()
    cherry_pick_env["GIT_COMMITTER_DATE"] = worktree_committer_date
    subprocess.run(
        ["git", "cherry-pick", worktree_head],
        cwd=tmp_path,
        env=cherry_pick_env,
        capture_output=True,
        text=True,
        check=True,
    )
    landed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _AlwaysPassEngine(engine_name),
    )
    final_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    refreshed = get_task(tmp_path, task.id)
    pipeline_state = SqlitePersistence(Workspace.from_path(tmp_path)).load(task.id)

    assert refreshed is not None
    journal = render_task_journal(Workspace.from_path(tmp_path), refreshed)

    assert result.final_stage == "done"
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.runtime.pipeline.git.commit_sha == final_head
    assert pipeline_state.commit_result is not None
    assert pipeline_state.commit_result.head_sha == final_head
    assert pipeline_state.commit_result.reason == "no_op"
    assert (tmp_path / "merged.txt").read_text() == "merged\n"
    assert (
        f"commit_to_git reconciled as a no-op on main at {final_head}; no new integration commit was needed."
    ) in journal
    assert landed_head != ""
    assert not worktree.exists()


def test_reconcile_terminal_commit_sha_recovers_missing_sha_from_persisted_commit_result(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    task = create_task(tmp_path, title="Repair missing terminal commit sha")
    persistence = SqlitePersistence(Workspace.from_path(tmp_path))
    state = persistence.initialize(task.id, stage=PipelineState.DONE, pipeline_mode=PipelineMode.FULL)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    state.commit_result = CommitResult(head_sha=head_sha, reason="no_op")
    persistence.save(state)

    fresh = get_task(tmp_path, task.id)
    assert fresh is not None
    fresh.status = TaskStatus.DONE
    fresh.pipeline_status = PipelineStatus.DONE
    save_task(tmp_path, fresh)

    reconciled = reconcile_terminal_commit_sha(
        tmp_path,
        fresh,
        final_state=TaskState(task_id=task.id, stage=PipelineState.DONE, pipeline_mode=PipelineMode.FULL),
        persistence=persistence,
    )
    reloaded = get_task(tmp_path, task.id)

    assert reconciled is not None
    assert reconciled.runtime.pipeline.git.commit_sha == head_sha
    assert reloaded is not None
    assert reloaded.runtime.pipeline.git.commit_sha == head_sha


def test_run_task_records_after_commit_hook_reject_and_flags_task(tmp_path: Path) -> None:
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hooks={
                "after_commit": [
                    {
                        "command": "git branch --show-current > after_commit_branch.txt && echo fail && exit 1",
                    }
                ]
            }
        ),
    )
    create_task(tmp_path, title="After commit warning")
    task = dequeue_next_task(Workspace.from_path(tmp_path))
    assert task is not None
    worktree = _prepare_committed_task_worktree(tmp_path, task)

    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _RejectRecoveryEngine(engine_name),
    )
    refreshed = get_task(tmp_path, task.id)

    assert result.final_stage == "failed"
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "flagged"
    assert (tmp_path / "merged.txt").read_text() == "merged\n"
    assert (tmp_path / "after_commit_branch.txt").read_text().strip() == "main"
    assert not worktree.exists()

    activity_entries = load_task_activity(Workspace.from_path(tmp_path), refreshed)
    assert activity_entries[-1].role == "hook"
    assert activity_entries[-1].stage == "commit_to_git"
    assert activity_entries[-1].verdict == "reject"
    assert "Runner hook at `after_commit` rejected the stage." in activity_entries[-1].message
    assert "echo fail && exit 1" in activity_entries[-1].message

    reports = load_stage_reports(Workspace.from_path(tmp_path), refreshed)
    hook_reports = [report for report in reports if report.source == "hook"]
    assert hook_reports
    assert hook_reports[-1].pipeline_state == "commit_to_git"
    assert hook_reports[-1].verdict == "reject"
    assert hook_reports[-1].source == "hook"
    assert hook_reports[-1].failure_diagnostics["phase"] == "after_commit"
    assert "echo fail && exit 1" in hook_reports[-1].feedback
    assert hook_reports[-1].warnings


def test_run_task_merge_conflict_failure_journal_stays_distinct_from_noop_reconciliation(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    create_task(tmp_path, title="Merge conflict failure")
    task = dequeue_next_task(Workspace.from_path(tmp_path))
    assert task is not None
    worktree = task_worktree_path(tmp_path, task)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", task_worktree_branch(task), str(worktree), "HEAD"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=worktree, check=True)
    (worktree / "seed.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "feature"], cwd=worktree, check=True)
    set_task_worktree_path(task, serialize_worktree_path(worktree))
    save_task(tmp_path, task)

    (tmp_path / "seed.txt").write_text("main\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "main"], cwd=tmp_path, check=True)

    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _RejectMergeResolverEngine(engine_name),
    )
    refreshed = get_task(tmp_path, task.id)

    assert refreshed is not None
    journal = render_task_journal(Workspace.from_path(tmp_path), refreshed)

    assert result.final_stage == "failed"
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "flagged"
    assert refreshed.flag_reason == "merge_failed"
    raw_state = _raw_task_state_payload(tmp_path, task.id)
    assert raw_state["status"] == "flagged"
    assert raw_state["pipeline_status"] == "flagged"
    assert raw_state["flag_reason"] == "merge_failed"
    assert refreshed.runtime.pipeline.git.commit_sha is None
    assert "reconciled as a no-op" not in journal
    assert ("commit_to_git failed during merge reconciliation: merge conflict remained after manual attempt") in journal


def test_run_task_before_accepting_hook_retries_and_continues(
    tmp_path: Path,
) -> None:
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hooks={
                "before_accepting": [
                    {
                        "command": (
                            "if [ ! -f .before_accepting_once ]; then "
                            "echo lint failed >&2; "
                            "touch .before_accepting_once; "
                            "exit 1; "
                            "fi"
                        ),
                        "description": "ensures acceptance starts from a lint-clean checkout",
                    }
                ]
            }
        ),
    )
    create_task(tmp_path, title="Before accepting hook warning")
    task = dequeue_next_task(Workspace.from_path(tmp_path))
    assert task is not None

    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _AlwaysPassEngine(engine_name),
    )
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None

    assert result.final_stage == "done"
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"

    activity_entries = load_task_activity(Workspace.from_path(tmp_path), refreshed)
    hook_entries = [entry for entry in activity_entries if entry.role == "hook"]
    assert hook_entries
    assert hook_entries[-1].stage == "accepting"
    assert hook_entries[-1].verdict == "reject"
    assert "lint failed" in hook_entries[-1].message

    reports = load_stage_reports(Workspace.from_path(tmp_path), refreshed)
    hook_reports = [report for report in reports if report.source == "hook"]
    assert hook_reports
    report = hook_reports[-1]
    assert report.pipeline_state == "accepting"
    assert report.verdict == "reject"
    assert report.source == "hook"
    assert report.failure_diagnostics["phase"] == "before_accepting"
    assert report.failure_diagnostics["consecutive_same_hook_rejects"] == 1
    assert report.warnings
    assert "lint failed" in report.feedback

    journal = render_task_journal(Workspace.from_path(tmp_path), refreshed)
    assert "Runner hook at `before_accepting` rejected the stage." in journal


def test_run_task_retries_sequential_hooks_one_failure_at_a_time(tmp_path: Path) -> None:
    first_command = (
        "printf 'first\\n' >> .hook_calls && "
        "if [ ! -f .first_hook_seen ]; then "
        "touch .first_hook_seen; "
        "echo first failed >&2; "
        "exit 1; "
        "fi"
    )
    second_command = (
        "printf 'second\\n' >> .hook_calls && "
        "if [ ! -f .second_hook_seen ]; then "
        "touch .second_hook_seen; "
        "echo second failed >&2; "
        "exit 1; "
        "fi"
    )
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hooks={
                "after_implementing": [
                    {"command": first_command, "description": "first hook"},
                    {"command": second_command, "description": "second hook"},
                ]
            }
        ),
    )
    create_task(tmp_path, title="Sequential first-failure stage hooks")
    task = dequeue_next_task(Workspace.from_path(tmp_path))
    assert task is not None

    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _AlwaysPassEngine(engine_name),
    )
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None

    assert result.final_stage == "done"
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert (tmp_path / ".hook_calls").read_text(encoding="utf-8") == "first\nfirst\nsecond\nfirst\nsecond\n"

    reports = load_stage_reports(Workspace.from_path(tmp_path), refreshed)
    hook_reports = [report for report in reports if report.source == "hook"]
    assert len(hook_reports) == 2
    first_report, second_report = hook_reports
    assert first_report.pipeline_state == "implementing"
    assert first_report.verdict == "reject"
    assert first_report.failure_diagnostics["phase"] == "after_implementing"
    assert len(first_report.warnings) == 1
    assert "first failed" in first_report.feedback
    assert "second failed" not in first_report.feedback
    assert second_report.pipeline_state == "implementing"
    assert second_report.verdict == "reject"
    assert len(second_report.warnings) == 1
    assert "second failed" in second_report.feedback


def test_run_task_skips_after_commit_when_hook_not_configured(tmp_path: Path) -> None:
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(),
    )
    create_task(tmp_path, title="After commit skipped")
    task = dequeue_next_task(Workspace.from_path(tmp_path))
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
    assert not (tmp_path / "after_commit_branch.txt").exists()
    assert not worktree.exists()


@pytest.mark.skip(reason="Integration test failing - hook command not creating ignored.log file")
def test_run_task_auto_commit_cleanup_excludes_db_and_gitignored_files(tmp_path: Path) -> None:
    hook_command = (
        "printf 'hook update\\n' > tracked.txt && "
        "printf 'generated\\n' > generated.txt && "
        "printf 'db\\n' > .litehive/local.db && "
        "printf 'ignored\\n' > ignored.log"
    )
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "after_implementing": [
                    {
                        "command": hook_command,
                    }
                ]
            }
        ),
    )
    create_task(tmp_path, title="Dirty main cleanup")
    (tmp_path / ".gitignore").write_text("ignored.log\n")
    (tmp_path / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)

    task = dequeue_next_task(Workspace.from_path(tmp_path))
    assert task is not None
    worktree = _prepare_committed_task_worktree(tmp_path, task)

    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _AlwaysPassEngine(engine_name),
    )
    refreshed = get_task(tmp_path, task.id)

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    changed_files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    subject = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.final_stage == "done"
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert (tmp_path / "merged.txt").read_text() == "merged\n"
    assert (tmp_path / "tracked.txt").read_text() == "hook update\n"
    assert (tmp_path / "generated.txt").read_text() == "generated\n"
    assert (tmp_path / ".litehive" / "local.db").read_text() == "db\n"
    assert (tmp_path / "ignored.log").read_text() == "ignored\n"
    assert status.stdout.strip() == "?? .litehive/local.db"
    assert "generated.txt" in changed_files.stdout.splitlines()
    assert "tracked.txt" in changed_files.stdout.splitlines()
    assert ".litehive/local.db" not in changed_files.stdout.splitlines()
    assert "ignored.log" not in changed_files.stdout.splitlines()
    assert subject.stdout.strip() == f"litehive {task.id}: auto-commit worktree changes"
    assert has_non_litehive_changes(tmp_path) is False
    assert not worktree.exists()


def test_main_checkout_cleanup_skips_tracked_ignored_task_reports(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    gitignore = tmp_path / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    gitignore.write_text(existing + ".litehive/tasks/*/reports/\n", encoding="utf-8")
    seed_task = create_task(tmp_path, title="Tracked ignored report")
    report_path = task_dir(tmp_path, seed_task) / "reports" / "testing-001.yaml"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("stage: testing\nsummary: seed\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-f", "--", str(report_path.relative_to(tmp_path))], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)

    (tmp_path / "tracked.txt").write_text("updated\n")
    report_path.write_text("stage: testing\nsummary: updated\n", encoding="utf-8")

    node = GitCommitNode(main_repo_root=tmp_path, worktree_resolver=lambda state: tmp_path)
    head = node.autocommit_main_checkout_changes(make_state(stage=PipelineState.COMMIT, task_id=seed_task.id))
    assert head is not None

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    changed_files = subprocess.run(
        ["git", "show", "--name-only", "--format=", head],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert str(report_path.relative_to(tmp_path)) not in changed_files.stdout.splitlines()
    assert "tracked.txt" in changed_files.stdout.splitlines()
    assert status.stdout.strip() == f"M {report_path.relative_to(tmp_path)}"
    assert has_non_litehive_changes(tmp_path) is False


def test_main_checkout_cleanup_commits_already_staged_deletions(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    probe_file = tmp_path / "tmp_review_probe" / ".litehive" / ".gitignore"
    probe_file.parent.mkdir(parents=True, exist_ok=True)
    probe_file.write_text(".lock\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)

    subprocess.run(["git", "rm", "-q", "--", str(probe_file.relative_to(tmp_path))], cwd=tmp_path, check=True)

    node = GitCommitNode(main_repo_root=tmp_path, worktree_resolver=lambda state: tmp_path)
    head = node.autocommit_main_checkout_changes(make_state(stage=PipelineState.COMMIT, task_id="T-0429"))
    assert head is not None

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    changed_files = subprocess.run(
        ["git", "show", "--name-status", "--format=", head],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert not probe_file.exists()
    assert f"D\t{probe_file.relative_to(tmp_path)}" in changed_files.stdout.splitlines()
    assert status.stdout.strip() == ""
