"""Tests for SubprocessHookRunner and GitCommitNode (real git merge path)."""

import subprocess
from pathlib import Path

import pytest

from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.lifecycle.events import HookOk, MergeConflictDetected, Pass, Reject
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.nodes.hook import ExecutionMode, HookNode, HookResult, HookRunner, HookSpec, SubprocessHookRunner
from litehive.lifecycle.nodes.system import GitCommitNode, StubCommitNode
from litehive.lifecycle.orchestration import run_task
from litehive.lifecycle.persistence import SqlitePersistence, TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.state.persist import load_state
from litehive.state.records import create_task, get_task, save_task, set_task_worktree_path
from litehive.tasks.queue import dequeue_next_task
from litehive.tasks.paths import task_dir
from litehive.tasks.reports import load_stage_reports, load_task_activity
from litehive.tasks.worktrees import serialize_worktree_path, task_worktree_branch, task_worktree_path

pytestmark = pytest.mark.integration


def make_state(stage: str = "before_grooming", task_id: str = "T-0001") -> TaskState:
    return TaskState(task_id=task_id, stage=stage, pipeline_mode=PipelineMode.FULL)


class SequenceHookRunner(HookRunner):
    def __init__(self, outcomes: dict[str, HookResult]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def run(self, spec: HookSpec, state: TaskState) -> HookResult:
        self.calls.append(spec.command)
        outcome = self.outcomes[spec.command]
        return HookResult(
            spec=spec,
            ok=outcome.ok,
            output=outcome.output,
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
        )


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


def test_hook_node_reject_includes_command_exit_code_and_streams(tmp_path: Path) -> None:
    node = HookNode(
        "after_implementing",
        hooks=[
            HookSpec(
                command="sh -c 'echo stdout-line && echo stderr-line >&2 && exit 3'",
                description="captures stdout and stderr",
                instructions_on_failure="fix the lint issue before retrying",
            )
        ],
        runner=SubprocessHookRunner(tmp_path),
    )

    event = node.run(make_state(stage="after_implementing"))

    assert isinstance(event, Reject)
    assert event.source == "hook"
    assert "Command: sh -c 'echo stdout-line && echo stderr-line >&2 && exit 3'" in event.reason
    assert "Exit code: 3" in event.reason
    assert "stdout-line" in event.reason
    assert "stderr-line" in event.reason
    assert event.metadata["hook_results"][0]["command"] == "sh -c 'echo stdout-line && echo stderr-line >&2 && exit 3'"
    assert event.metadata["hook_results"][0]["exit_code"] == 3
    assert event.metadata["hook_results"][0]["stdout"] == "stdout-line"
    assert event.metadata["hook_results"][0]["stderr"] == "stderr-line"


def test_hook_node_default_runs_all_hooks_and_reports_all_blocking_failures() -> None:
    hooks = [
        HookSpec(command="first", reject_on_failure=True, description="lint"),
        HookSpec(command="second", reject_on_failure=True, description="tests"),
        HookSpec(command="third", reject_on_failure=True, description="typing"),
    ]
    runner = SequenceHookRunner(
        {
            "first": HookResult(
                spec=hooks[0],
                ok=False,
                exit_code=1,
                stdout="lint stdout",
                stderr="lint stderr",
            ),
            "second": HookResult(
                spec=hooks[1],
                ok=False,
                exit_code=2,
                stdout="tests stdout",
                stderr="tests stderr",
            ),
            "third": HookResult(
                spec=hooks[2],
                ok=True,
                exit_code=0,
                stdout="typing ok",
            ),
        }
    )
    node = HookNode("after_implementing", hooks=hooks, runner=runner)

    event = node.run(make_state(stage="after_implementing"))

    assert isinstance(event, Reject)
    assert runner.calls == ["first", "second", "third"]
    assert len(event.metadata["hook_results"]) == 2
    assert [result["command"] for result in event.metadata["hook_results"]] == ["first", "second"]
    assert "Command: first" in event.reason
    assert "lint stdout" in event.reason
    assert "lint stderr" in event.reason
    assert "Command: second" in event.reason
    assert "tests stdout" in event.reason
    assert "tests stderr" in event.reason


def test_hook_node_fail_fast_stops_on_first_blocking_failure() -> None:
    hooks = [
        HookSpec(command="first", reject_on_failure=True),
        HookSpec(command="second", reject_on_failure=True),
    ]
    runner = SequenceHookRunner(
        {
            "first": HookResult(spec=hooks[0], ok=False, exit_code=1, stderr="first failed"),
            "second": HookResult(spec=hooks[1], ok=False, exit_code=2, stderr="second failed"),
        }
    )
    node = HookNode(
        "after_implementing",
        hooks=hooks,
        runner=runner,
        execution_mode=ExecutionMode.FAIL_FAST,
    )

    event = node.run(make_state(stage="after_implementing"))

    assert isinstance(event, Reject)
    assert runner.calls == ["first"]
    assert len(event.metadata["hook_results"]) == 1
    assert event.metadata["hook_results"][0]["command"] == "first"
    assert "first failed" in event.reason
    assert "second failed" not in event.reason


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
    (repo / ".litehive" / "tasks" / "T-0001-demo" / "task.yaml").write_text("id: T-0001\nstatus: in_progress\n")

    # Worktree: SWE writes real code AND the runner updates task metadata there too.
    (worktree / "new.txt").write_text("agent wrote this\n")
    (worktree / ".litehive" / "tasks" / "T-0001-demo" / "task.yaml").write_text("id: T-0001\nstatus: implementing\n")

    node = GitCommitNode(repo, worktree_resolver=lambda state: worktree)
    event = node.run(make_state(stage="commit", task_id="T-0001"))

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
    event = node.run(make_state(stage="commit"))

    assert isinstance(event, Pass), event
    assert (repo / "new.txt").read_text() == "agent wrote this\n"
    # Main's uv.lock must remain the main-side version — not overwritten by the worktree churn.
    assert (repo / "uv.lock").read_text() == "version = 1\n"


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
                "role": prompt["role"],
                "pipeline_mode": prompt["pipeline_mode"],
                "task_id": prompt["task_id"],
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
    task = dequeue_next_task(tmp_path)
    assert task is not None

    calls: list[dict[str, str]] = []
    result = run_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _RecordingPassEngine(engine_name, calls),
    )
    refreshed = get_task(tmp_path, task.id)
    pipeline_state = SqlitePersistence(tmp_path).load(task.id)
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

    thread = load_task_activity(tmp_path, refreshed)
    assert thread[-1].role == "hook"
    assert thread[-1].stage == "implementing"
    assert thread[-1].verdict == "reject"
    assert "fix the merged state" not in thread[-1].message
    assert "routing: implementing" in thread[-1].message

    reports = load_stage_reports(tmp_path, refreshed)
    hook_reports = [report for report in reports if report.source == "hook"]
    assert hook_reports
    assert hook_reports[-1].stage == "commit_to_git"
    assert hook_reports[-1].verdict == "reject"
    assert hook_reports[-1].source == "hook"
    assert hook_reports[-1].failure_diagnostics["phase"] == "after_merge"
    assert hook_reports[-1].failure_diagnostics["routed_to"] == "implementing"
    assert "Exit code: 1" in hook_reports[-1].feedback
    assert "echo fail && exit 1" in hook_reports[-1].feedback


def test_run_task_before_accepting_hook_retries_back_to_implementing_and_records_hook_report(
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
                        "blocking": True,
                        "description": "ensures acceptance starts from a lint-clean checkout",
                    }
                ]
            }
        ),
    )
    create_task(tmp_path, title="Before accepting hook retry")
    task = dequeue_next_task(tmp_path)
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

    transitions = SqlitePersistence(tmp_path).load(task.id)
    assert transitions.stage == "done"
    assert transitions.last_rejection_by_stage["accepting"].source == "hook"
    assert transitions.last_rejection_by_stage["accepting"].raised_at_phase == "before_accepting"

    thread = load_task_activity(tmp_path, refreshed)
    hook_entries = [entry for entry in thread if entry.role == "hook"]
    assert hook_entries
    assert hook_entries[-1].stage == "accepting"
    assert hook_entries[-1].verdict == "reject"
    assert "routing: implementing" in hook_entries[-1].message

    reports = load_stage_reports(tmp_path, refreshed)
    hook_reports = [report for report in reports if report.source == "hook"]
    assert hook_reports
    report = hook_reports[-1]
    assert report.stage == "accepting"
    assert report.verdict == "reject"
    assert report.source == "hook"
    assert report.failure_diagnostics["phase"] == "before_accepting"
    assert report.failure_diagnostics["routed_to"] == "implementing"
    assert report.hook_results[0]["exit_code"] == 1
    assert report.hook_results[0]["stderr"] == "lint failed"
    assert "Exit code: 1" in report.feedback
    assert "lint failed" in report.feedback

    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Runner hook at `before_accepting` rejected the task." in journal
    assert "routing: `implementing`" in journal


def test_run_task_aggregates_all_stage_hook_failures_by_default(tmp_path: Path) -> None:
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
                    {"command": first_command, "blocking": True, "description": "first blocking hook"},
                    {"command": second_command, "blocking": True, "description": "second blocking hook"},
                ]
            }
        ),
    )
    create_task(tmp_path, title="Run-all stage hook aggregation")
    task = dequeue_next_task(tmp_path)
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
    assert (tmp_path / ".hook_calls").read_text(encoding="utf-8") == "first\nsecond\nfirst\nsecond\n"

    reports = load_stage_reports(tmp_path, refreshed)
    hook_reports = [report for report in reports if report.source == "hook"]
    assert len(hook_reports) == 1
    report = hook_reports[0]
    assert report.stage == "implementing"
    assert report.failure_diagnostics["phase"] == "after_implementing"
    assert [result["command"] for result in report.hook_results] == [first_command, second_command]
    assert report.hook_results[0]["stderr"] == "first failed"
    assert report.hook_results[1]["stderr"] == "second failed"
    assert "first failed" in report.feedback
    assert "second failed" in report.feedback


def test_run_task_honors_fail_fast_stage_hook_mode_from_config(tmp_path: Path) -> None:
    _init_workspace_git_repo(
        tmp_path,
        config=LitehiveConfig(
            runner_hook_execution_mode="fail_fast",
            runner_hooks={
                "after_implementing": [
                    {
                        "command": (
                            "printf 'first\\n' >> .hook_calls && "
                            "if [ ! -f .first_hook_seen ]; then "
                            "touch .first_hook_seen; "
                            "echo first failed >&2; "
                            "exit 1; "
                            "fi"
                        ),
                        "blocking": True,
                        "description": "first blocking hook",
                    },
                    {
                        "command": "printf 'second\\n' >> .hook_calls",
                        "blocking": True,
                        "description": "second blocking hook",
                    },
                ]
            },
        ),
    )
    create_task(tmp_path, title="Fail-fast stage hook wiring")
    task = dequeue_next_task(tmp_path)
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
    assert (tmp_path / ".hook_calls").read_text(encoding="utf-8") == "first\nfirst\nsecond\n"

    reports = load_stage_reports(tmp_path, refreshed)
    hook_reports = [report for report in reports if report.source == "hook"]
    assert len(hook_reports) == 1
    report = hook_reports[0]
    assert report.stage == "implementing"
    assert report.failure_diagnostics["phase"] == "after_implementing"
    assert len(report.hook_results) == 1
    assert report.hook_results[0]["command"] == (
        "printf 'first\\n' >> .hook_calls && "
        "if [ ! -f .first_hook_seen ]; then "
        "touch .first_hook_seen; "
        "echo first failed >&2; "
        "exit 1; "
        "fi"
    )
    assert report.hook_results[0]["stderr"] == "first failed"
    assert "first failed" in report.feedback
    assert "second" not in report.feedback


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
