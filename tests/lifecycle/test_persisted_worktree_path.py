import subprocess
from pathlib import Path

from heru.types import SubagentRef

from litehive.config.workspace import ensure_workspace
from litehive.domain.agent import SubagentResult
from litehive.lifecycle.heru_factory import HeruEngineAdapter
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.nodes.system import GitCommitNode, GitWorktreeSyncNode
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.sessions import Session
from litehive.lifecycle.types import PipelineMode
from litehive.lifecycle.events import Pass
from litehive.state.records import create_task, get_task_worktree_path, require_task
from litehive.state.store import runtime_store
from litehive.worktree import resolve_recorded_worktree_path, task_worktree_branch, task_worktree_path


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


def _state(task_id: str, stage: str) -> TaskState:
    return TaskState(task_id=task_id, stage=stage, pipeline_mode=PipelineMode.FULL)


class _StubManager:
    last_init: tuple[Path, Path] | None = None

    def __init__(self, workspace_root, *, execution_root=None):
        _StubManager.last_init = (Path(workspace_root).resolve(), Path(execution_root).resolve())

    def run(self, task, **kwargs) -> SubagentResult:
        del task, kwargs
        return SubagentResult(
            ref=SubagentRef(
                id="SA-0001",
                role="swe",
                engine="codex",
                status="completed",
                path="subagents/SA-0001-swe",
            ),
            execution=None,
            transcript="",
            exit_code=0,
            continuation=None,
        )


def test_worktree_sync_persists_runtime_worktree_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git_ok(workspace, "add", "seed.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Persist worktree")
    worktree = task_worktree_path(workspace, task)
    node = GitWorktreeSyncNode(
        workspace_root=workspace,
        worktree_resolver=lambda state: worktree,
    )

    changed = node._sync(_state(task.id, "worktree_sync"))

    stored = runtime_store(workspace).load_task_runtime(task.id)
    assert changed is True
    assert stored is not None
    assert stored.git.worktree_path is not None
    resolved = resolve_recorded_worktree_path(workspace, stored.git.worktree_path)
    assert resolved is not None
    assert resolved.exists()


def test_agent_and_commit_use_persisted_worktree_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git_ok(workspace, "add", "seed.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Persisted checkout")
    worktree = task_worktree_path(workspace, task)
    sync_node = GitWorktreeSyncNode(
        workspace_root=workspace,
        worktree_resolver=lambda state: worktree,
    )
    assert sync_node._sync(_state(task.id, "worktree_sync")) is True

    adapter = HeruEngineAdapter("codex", workspace)
    session = Session()
    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    verdict = adapter.run_turn(
        session,
        {
            "task_id": task.id,
            "stage": "implementing",
            "role": "swe",
            "pipeline_mode": "full",
            "instructions": [],
        },
        _state(task.id, "implementing"),
    )

    assert verdict.outcome == "pass"
    assert _StubManager.last_init == (workspace.resolve(), worktree.resolve())

    (worktree / "new.txt").write_text("agent wrote this\n", encoding="utf-8")

    def _persisted_worktree(state: TaskState) -> Path:
        recorded_task = require_task(workspace, state.task_id)
        recorded = get_task_worktree_path(recorded_task)
        assert recorded is not None
        resolved = resolve_recorded_worktree_path(workspace, recorded)
        assert resolved is not None
        return resolved

    commit_node = GitCommitNode(workspace, worktree_resolver=_persisted_worktree)
    event = commit_node.run(_state(task.id, "commit"))

    assert isinstance(event, Pass), event
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "agent wrote this\n"
    assert _git_ok(workspace, "log", "-1", "--format=%s", task_worktree_branch(task)) == (
        f"litehive {task.id}: auto-commit worktree changes"
    )


def test_commit_ignores_untracked_embedded_git_repos_in_task_worktree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git_ok(workspace, "add", "seed.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Scratch repo in worktree")
    worktree = task_worktree_path(workspace, task)
    sync_node = GitWorktreeSyncNode(
        workspace_root=workspace,
        worktree_resolver=lambda state: worktree,
    )
    assert sync_node._sync(_state(task.id, "worktree_sync")) is True

    (worktree / "new.txt").write_text("agent wrote this\n", encoding="utf-8")
    scratch_repo = worktree / "$workspace"
    scratch_repo.mkdir()
    _git_ok(scratch_repo, "init", "-b", "main")

    def _persisted_worktree(state: TaskState) -> Path:
        recorded_task = require_task(workspace, state.task_id)
        recorded = get_task_worktree_path(recorded_task)
        assert recorded is not None
        resolved = resolve_recorded_worktree_path(workspace, recorded)
        assert resolved is not None
        return resolved

    commit_node = GitCommitNode(workspace, worktree_resolver=_persisted_worktree)
    event = commit_node.run(_state(task.id, "commit"))

    assert isinstance(event, Pass), event
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "agent wrote this\n"
    assert not (workspace / "$workspace").exists()
    assert _git_ok(workspace, "log", "-1", "--format=%s", task_worktree_branch(task)) == (
        f"litehive {task.id}: auto-commit worktree changes"
    )
    assert _git_ok(worktree, "status", "--short") == "?? $workspace/"
