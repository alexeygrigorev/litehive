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
from .engines import ConfigBackedEngineSelector, EngineFactory
from .heru_factory import heru_engine_factory
from .journal import SqliteJournal
from .nodes import (
    GitCommitNode,
    GitWorktreeSyncNode,
    HookSpec,
    PreExecRecoveryNode,
    ReadyNode,
    SubprocessHookRunner,
)
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


def _resolve_worktree(root: Path, state: TaskState) -> Path:
    """Look up the on-disk worktree path for a task, falling back to root."""
    from litehive.tasks.crud import get_task as _get_task
    from litehive.tasks.worktrees import resolve_recorded_worktree_path

    task = _get_task(root, state.task_id)
    if task is None:
        return root
    wt = get_task_worktree_path(task)
    if not wt:
        return root
    return resolve_recorded_worktree_path(root, wt) or root


def _build_commit_node(root: Path) -> CommitNode:
    """Return the production ``GitCommitNode`` bound to this workspace."""
    return GitCommitNode(root, worktree_resolver=lambda state: _resolve_worktree(root, state))


def _build_worktree_sync_node(root: Path) -> GitWorktreeSyncNode:
    """Return the production ``GitWorktreeSyncNode`` bound to this workspace."""
    return GitWorktreeSyncNode(
        worktree_resolver=lambda state: _resolve_worktree(root, state),
    )


def _missing_worktree_probe(root: Path):
    """Return a probe callable that flags tasks whose worktree_path is gone."""

    from litehive.tasks.crud import get_task as _get_task
    from litehive.tasks.worktrees import resolve_recorded_worktree_path

    def _probe(state) -> bool:
        task = _get_task(root, state.task_id)
        if task is None:
            return False
        wt = get_task_worktree_path(task)
        if not wt:
            return False
        path = resolve_recorded_worktree_path(root, wt)
        if path is None:
            return False
        return not path.exists()

    return _probe


def _clear_stale_worktree_repair(root: Path):
    """Return a repair callable that clears a stale worktree_path on the task."""

    from litehive.tasks.crud import get_task as _get_task
    from litehive.tasks.crud import set_task_worktree_path, save_task
    from litehive.tasks.worktrees import resolve_recorded_worktree_path

    def _repair(state) -> None:
        task = _get_task(root, state.task_id)
        if task is None:
            return
        wt = get_task_worktree_path(task)
        if not wt:
            return
        path = resolve_recorded_worktree_path(root, wt)
        if path is not None and path.exists():
            return
        set_task_worktree_path(task, None)
        save_task(root, task)

    return _repair


def _hook_specs_from_config(config) -> dict[str, list[HookSpec]]:
    """Translate ``LitehiveConfig.runner_hooks`` into v2 ``HookSpec`` lists.

    v1 config stores runner hooks as ``dict[phase_name, list[HookConfig]]``
    where HookConfig has ``command``, ``reject_on_failure``,
    ``timeout_seconds``, ``description``, ``instructions_on_failure``. v2
    HookSpec is a strict subset — just command / reject_on_failure /
    timeout_seconds. The phase names match (``before_grooming``,
    ``after_implementing``, …), so this is a straight per-phase rewrite.
    """
    out: dict[str, list[HookSpec]] = {}
    raw = getattr(config, "runner_hooks", None) or {}
    for phase, hooks in raw.items():
        specs: list[HookSpec] = []
        for hook in hooks or []:
            specs.append(
                HookSpec(
                    command=hook.command,
                    reject_on_failure=bool(getattr(hook, "reject_on_failure", True)),
                    timeout_seconds=int(getattr(hook, "timeout_seconds", 60) or 60),
                )
            )
        if specs:
            out[phase] = specs
    return out


def run_task_v2(
    root: Path,
    task: TaskRecord,
    *,
    engine_factory: EngineFactory | None = None,
) -> ExecutionResultV2:
    """Run a single task through the v2 state machine.

    Takes the workspace runner guard and publishes a heartbeat so other
    tools see the task as active. Always uses the real ``GitCommitNode``
    — v2 is the executor, not a dry run.

    ``engine_factory`` is an injection point for tests: pass a callable
    that produces fake ``Engine`` instances and v2 will use it in place
    of the real ``heru_engine_factory``.
    """
    root = root.resolve()
    config = load_config(root)

    with workspace_runner_guard(root):
        # 1. Make sure the v2 state row exists for this task.
        load_or_initialize(task.id, root)

        # 2. Build dependencies. Engine selector + heru factory wire up
        #    the real engines via the existing SubagentManager; the rest
        #    are the sqlite stores we land in M1.
        factory = engine_factory or heru_engine_factory(root)
        selector = ConfigBackedEngineSelector(config, factory)
        sessions = SqliteSessionStore(root)
        persistence = SqlitePersistence(root)
        journal = SqliteJournal(root)
        hook_runner = SubprocessHookRunner(root)
        commit_node = _build_commit_node(root)
        worktree_sync_node = _build_worktree_sync_node(root)
        ready_node = ReadyNode(probes=[_missing_worktree_probe(root)])
        pre_exec_recovery_node = PreExecRecoveryNode(
            repairs=[_clear_stale_worktree_repair(root)],
        )
        prompt_context = PromptContext(workspace_root=root)
        hook_specs = _hook_specs_from_config(config)

        registry = build_registry(
            selector=selector,
            session_store=sessions,
            hook_runner=hook_runner,
            commit_node=commit_node,
            worktree_sync_node=worktree_sync_node,
            ready_node=ready_node,
            pre_exec_recovery_node=pre_exec_recovery_node,
            prompt_context=prompt_context,
            hook_specs=hook_specs,
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
