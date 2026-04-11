"""v2 task orchestration entry point.

One function — ``run_task_v2(root, task)`` — that wires up the v2 pipeline
end-to-end and drives one task through the state machine. It is the
v2-equivalent of ``pipeline_old._orchestration.run_task`` and exists so the
daemon (or any caller) can flip a single line to opt in to v2.

What it does, in order:

1. Loads workspace config.
2. Initializes (or loads) the v2 ``TaskState`` via the v1 bridge so the
   sqlite row exists with the right pipeline mode.
3. Constructs the engine selector / session store / persistence /
   journal / hook runner / commit node.
4. Builds the full ``NodeRegistry``.
5. Runs ``StateMachineRunner.run_task(task_id)``.
6. Syncs the v2 terminal state back to the v1 ``TaskRecord`` so
   ``litehive status`` and the queue stay coherent.

Returns a small ``ExecutionResultV2`` named-tuple-ish dataclass the
caller can render.
"""

from dataclasses import dataclass
from pathlib import Path

from litehive.config import load_config
from litehive.models import TaskRecord
from litehive.tasks.crud import get_task_worktree_path
from litehive.workspace.locking import runner_heartbeat, workspace_runner_guard

from .agents._base import PromptContext
from .engines import ConfigBackedEngineSelector
from .heru_factory import heru_engine_factory
from .journal import SqliteJournal
from .nodes import GitCommitNode, StubCommitNode, SubprocessHookRunner
from .nodes.system import CommitNode
from .persistence import SqlitePersistence, TaskState
from .registry import build_registry
from .runner import StateMachineRunner
from .sessions import SqliteSessionStore
from .v1_bridge import load_or_initialize, sync_back_to_task_record


@dataclass
class ExecutionResultV2:
    """Result of running one task through the v2 state machine."""

    task: TaskRecord | None
    final_state: TaskState | None
    final_stage: str
    failed_reason: str | None = None
    failed_message: str | None = None


def _build_commit_node(root: Path, *, stub: bool) -> CommitNode:
    """Return a commit node for the runner.

    ``stub=True`` → always-pass ``StubCommitNode`` (safe for dry runs or
    first-time smoke testing of v2 against real tasks). Default is the
    real ``GitCommitNode`` which performs an actual ``git merge``.
    """
    if stub:
        return StubCommitNode()

    from litehive.tasks.crud import get_task as _get_task

    def _resolve_worktree(state: TaskState) -> Path:
        task = _get_task(root, state.task_id)
        if task is None:
            return root
        wt = get_task_worktree_path(task)
        if not wt:
            return root
        path = Path(wt)
        if not path.is_absolute():
            path = root / path
        return path

    return GitCommitNode(root, worktree_resolver=_resolve_worktree)


def run_task_v2(
    root: Path,
    task: TaskRecord,
    *,
    stub_commit: bool = False,
) -> ExecutionResultV2:
    """Run a single task through the v2 state machine.

    Takes the workspace runner guard (same lock v1 uses) and publishes
    a heartbeat so other tools see the task as active.

    ``stub_commit=True`` swaps the real git merge for ``StubCommitNode``
    — use it for dry runs and early smoke testing before we're confident
    v2 won't wreck a worktree.
    """
    root = root.resolve()
    config = load_config(root)

    with workspace_runner_guard(root):
        # 1. Make sure the v2 state row exists for this task.
        load_or_initialize(task.id, root)

        # 2. Build dependencies. Engine selector + heru factory wire up
        #    the real engines via the existing SubagentManager; the rest
        #    are the sqlite stores we land in M1.
        selector = ConfigBackedEngineSelector(config, heru_engine_factory(root))
        sessions = SqliteSessionStore(root)
        persistence = SqlitePersistence(root)
        journal = SqliteJournal(root)
        hook_runner = SubprocessHookRunner(root)
        commit_node = _build_commit_node(root, stub=stub_commit)
        prompt_context = PromptContext(workspace_root=root)

        registry = build_registry(
            selector=selector,
            session_store=sessions,
            hook_runner=hook_runner,
            commit_node=commit_node,
            prompt_context=prompt_context,
        )

        runner = StateMachineRunner(
            registry,
            persistence,
            journal=journal,
        )

        # 3. Run under the heartbeat so `litehive status` sees the active task.
        with runner_heartbeat(root, active_task_id=task.id):
            final_state = runner.run_task(task.id)

        # 4. Mirror terminal state back to the v1 TaskRecord.
        updated_task = sync_back_to_task_record(final_state, root) or task

    return ExecutionResultV2(
        task=updated_task,
        final_state=final_state,
        final_stage=final_state.stage,
        failed_reason=final_state.failed_reason,
        failed_message=final_state.failed_message,
    )
