"""Full-bootstrap integration test: run_task against a real workspace.

Exercises the real stack — ``SqlitePersistence``, ``SqliteSessionStore``,
``SqliteJournal``, ``SubprocessHookRunner``, ``GitCommitNode`` via
``run_task`` — only stubbing the engine factory so we don't invoke
real LLM CLIs. Every other wiring layer is exactly what prod uses.

Two tests: one that drives a task straight to ``done`` by making every
stage autopass, one that asserts the engine-blocked cascade lands the
task in ``failed``.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from litehive.lifecycle.nodes.agent import AgentVerdict, EngineBlockedError
from litehive.lifecycle.orchestration import run_task_for_workspace
from litehive.state.records import create_task_for_workspace, save_task_for_workspace
from litehive.workspace import Workspace

from litehive.config.workspace import create_workspace

pytestmark = pytest.mark.integration


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / ".gitignore").write_text(".litehive/\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)


@pytest.fixture
def live_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _init_git_repo(root)
    create_workspace(root)
    return root


class _NamedEngine:
    """Thin engine wrapper whose ``name`` matches what the selector asked for.

    Critical for the excluded-set bookkeeping in AgentNode: the AgentNode
    adds ``engine.name`` to its ``excluded`` set after a tier-2 error, so
    the factory must stamp the requested name onto the returned object or
    the selector will hand back the same "unique" engine forever.
    """

    def __init__(self, name: str, behavior) -> None:
        self.name = name
        self._behavior = behavior

    def run_turn(self, session: Any, prompt: Any, state: Any) -> AgentVerdict:
        return self._behavior(self, session, prompt, state)


def _auto_pass_behavior(shared_calls: list) -> Any:
    def _run(engine, session, prompt, state) -> AgentVerdict:
        shared_calls.append((engine.name, state.stage))
        session.engine_session_id = f"stub-{state.task_id}-{state.stage}-{len(shared_calls)}"
        return AgentVerdict(outcome="pass", reason="stub-auto-pass")

    return _run


def _always_block_behavior(engine, session, prompt, state) -> AgentVerdict:
    raise EngineBlockedError(f"stub: engine {engine.name} blocked")


def _stub_factory(behavior):
    def _factory(engine_name: str):
        return _NamedEngine(engine_name, behavior)

    return _factory


# ── happy path ───────────────────────────────────────────────────────────


def test_run_task_happy_path_against_real_workspace(live_workspace: Path) -> None:
    workspace = Workspace.from_path(live_workspace)
    task = create_task_for_workspace(
        workspace,
        title="v2 bootstrap smoke",
        goal="make sure v2 end-to-end actually works on a real workspace",
        pipeline_mode="single",
    )
    # Seed a non-empty last_report expectation by setting plan so
    # single-mode routes through commit (not the zero-change shortcut).
    task.plan = ["step 1"]
    save_task_for_workspace(workspace, task)

    calls: list = []
    result = run_task_for_workspace(
        workspace,
        workspace.load_config(),
        task,
        engine_factory=_stub_factory(_auto_pass_behavior(calls)),
    )

    assert result.final_stage == "done", (
        f"expected done, got {result.final_stage!r} (reason={result.failed_reason!r}, msg={result.failed_message!r})"
    )
    assert len(calls) >= 1


# ── all engines blocked → failed ────────────────────────────────────────


def test_run_task_full_mode_walks_every_stage(live_workspace: Path) -> None:
    """Full-mode task: grooming → implementing → testing → accepting → done."""
    workspace = Workspace.from_path(live_workspace)
    task = create_task_for_workspace(
        workspace,
        title="full-mode smoke",
        goal="walk every agent stage",
        pipeline_mode="full",
    )

    calls: list = []
    result = run_task_for_workspace(
        workspace,
        workspace.load_config(),
        task,
        engine_factory=_stub_factory(_auto_pass_behavior(calls)),
    )

    assert result.final_stage == "done", (
        f"expected done, got {result.final_stage!r} (reason={result.failed_reason!r}, msg={result.failed_message!r})"
    )
    # Every agent stage must have run at least once
    stages_called = {stage for _, stage in calls}
    assert stages_called >= {"grooming", "implementing", "testing", "accepting"}


def test_run_task_all_engines_blocked_lands_in_failed(live_workspace: Path) -> None:
    workspace = Workspace.from_path(live_workspace)
    task = create_task_for_workspace(
        workspace,
        title="v2 bootstrap failure",
        goal="make sure a blocked engine cascades to failed",
        pipeline_mode="single",
    )

    result = run_task_for_workspace(
        workspace,
        workspace.load_config(),
        task,
        engine_factory=_stub_factory(_always_block_behavior),
    )

    assert result.final_stage == "failed"
    assert result.failed_reason is not None
