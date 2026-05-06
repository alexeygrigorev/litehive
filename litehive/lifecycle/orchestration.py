"""Task orchestration entry point.

One function — ``run_task(root, task)`` — that wires up the pipeline
end-to-end and drives one task through the state machine. It is the
single boundary the daemon and CLI both go through to start a task.

What it does, in order:

1. Loads workspace config.
2. Initializes (or loads) the ``TaskState`` via the bridge so the
   sqlite row exists with the right pipeline mode.
3. Constructs the engine selector / session store / persistence /
   journal / hook runner / commit node.
4. Builds the full ``NodeRegistry``.
5. Runs ``StateMachineRunner.run_task(task_id)``.
6. Syncs the terminal state back to the ``TaskRecord`` so
   ``litehive status`` and the queue stay coherent.

Returns a small ``ExecutionResult`` named-tuple-ish dataclass the
caller can render.
"""

from dataclasses import dataclass, replace
from pathlib import Path

from litehive.config.loading import load_config
from litehive.config.engine_models import resolve_task_rejection_loop_limit, resolve_task_retry_policy
from litehive.git.ops import GitError
from litehive.domain.common import PipelineState
from litehive.domain.task import TaskRecord
from litehive.state.records import get_task
from litehive.state.locking import runner_heartbeat, workspace_runner_guard

from litehive.roles.base import PromptContext
from .events import HookOk, Reject
from .engines import ConfigBackedEngineSelector, EngineFactory
from .heru_factory import heru_engine_factory
from .hook_reports import (
    _record_hook_reject,
    _record_hook_warnings,
    hook_specs_from_config,
)
from .journal import SqliteJournal
from .launch_state import _load_or_initialize
from .nodes.hook import SubprocessHookRunner
from .nodes.system import PreExecRecoveryNode, ReadyNode
from .persistence import Limits, SqlitePersistence, TaskState
from .registry import build_registry
from .runner import StateMachineRunner
from .runtime_sync import (
    _clear_terminal_task_from_workspace_state,
    _sync_back,
)
from .sessions import SqliteSessionStore
from litehive.workspace import Workspace
from .transitions import Transition
from .worktree_setup import (
    _build_worktree_sync_node,
    _cleanup_terminal_worktree,
    _mark_task_interrupted_on_crash,
    _resolve_hook_execution_root,
    _worktree_metadata_repair,
    _worktree_missing_probe,
    build_commit_node,
    reconcile_terminal_commit_sha,
)


__all__ = [
    "ExecutionResult",
    "_load_or_initialize",
    "_sync_back",
    "build_commit_node",
    "hook_specs_from_config",
    "reconcile_terminal_commit_sha",
    "run_task",
]


def _sync_back_no_return(state: TaskState, root: Path) -> None:
    """Adapter around ``_sync_back`` for the StateMachineRunner state_sync hook.

    ``_sync_back`` returns the updated TaskRecord for the orchestration code
    that calls it after the runner finishes; the runner's ``state_sync``
    parameter is typed as ``(TaskState) -> None`` because the callback's
    return value is never consumed.
    """
    _sync_back(state, root)


@dataclass
class ExecutionResult:
    """Result of running one task through the pipeline state machine."""

    task: TaskRecord | None
    final_state: TaskState | None
    final_stage: PipelineState
    failed_reason: str | None = None
    failed_message: str | None = None


def run_task(
    root: Path,
    task: TaskRecord,
    engine_factory: EngineFactory | None = None,
    engine_override: str | None = None,
    model_override: str | None = None,
) -> ExecutionResult:
    """
    Run a single task through the state machine.

    Takes the workspace runner guard and publishes a heartbeat so other
    tools (CLI status, daemon supervisors) see the task as active.
    Always uses the real ``GitCommitNode`` so tests that exercise the
    commit stage do touch git plumbing.

    ``engine_factory`` is an injection point for tests: pass a callable
    that produces fake ``Engine`` instances and the pipeline will use
    it in place of the real ``heru_engine_factory``.
    """
    root = root.resolve()
    config = load_config(root)
    workspace = Workspace.from_path(root)

    with workspace_runner_guard(root):
        persistence = SqlitePersistence(
            workspace,
            limits=replace(
                Limits(),
                rejection_loop_limit=resolve_task_rejection_loop_limit(task, config),
            ),
        )
        _load_or_initialize(task.id, root, persistence)

        factory = engine_factory or heru_engine_factory(root)
        selector = ConfigBackedEngineSelector(
            config,
            factory,
            workspace_root=root,
            engine_override=engine_override,
            model_override=model_override,
            check_quota=engine_factory is None,
        )
        sessions = SqliteSessionStore(workspace)
        journal = SqliteJournal(workspace)
        hook_runner = SubprocessHookRunner(
            root,
            execution_root_resolver=lambda state: _resolve_hook_execution_root(root, state),
        )
        commit_node = build_commit_node(root)
        worktree_sync_node = _build_worktree_sync_node(root)
        ready_node = ReadyNode(probes=[_worktree_missing_probe(root)])
        pre_exec_recovery_node = PreExecRecoveryNode(
            repairs=[_worktree_metadata_repair(root)],
        )
        prompt_context = PromptContext(workspace_root=root)
        hook_specs = hook_specs_from_config(config)
        retry_budget = resolve_task_retry_policy(task, config)

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
            retry_budget=retry_budget,
            retry_on=tuple(config.retry_on),
        )

        runner = StateMachineRunner(
            registry,
            persistence,
            journal=journal,
            state_sync=lambda state: _sync_back_no_return(state, root),
            transition_observer=lambda state, from_stage, event, trans: _observe_transition(
                workspace,
                state,
                from_stage,
                event,
                trans,
            ),
            session_store=sessions,
            task_time_budget_seconds=config.task_time_budget_seconds,
        )

        # 3. Run under the heartbeat so `litehive status` sees the active task.
        with runner_heartbeat(root, active_task_id=task.id):
            try:
                final_state = runner.run_task(task.id)
            except BaseException:
                # Runner crashed — mark task as interrupted so it can be
                # resumed instead of leaving stale "running" state behind.
                _mark_task_interrupted_on_crash(root, task)
                raise

        # 4. Mirror terminal state back to the TaskRecord.
        updated_task = _sync_back(final_state, root) or task
        if final_state.stage in {PipelineState.DONE, PipelineState.FAILED}:
            reconciled_task = reconcile_terminal_commit_sha(
                root,
                updated_task,
                final_state=final_state,
                persistence=persistence,
            )
            updated_task = reconciled_task or updated_task
            _clear_terminal_task_from_workspace_state(root, updated_task.id)
            try:
                _cleanup_terminal_worktree(root, updated_task)
            except GitError:
                pass

    return ExecutionResult(
        task=updated_task,
        final_state=final_state,
        final_stage=final_state.stage,
        failed_reason=final_state.failed_reason,
        failed_message=final_state.failed_message,
    )


def _observe_transition(
    workspace: Workspace,
    state: TaskState,
    from_stage: str,
    event: object,
    trans: Transition,
) -> None:
    """
    Turn hook outcomes into stage reports plus journal/activity entries.

    Only hook events produce reports here; engine events are reported
    by the nodes themselves. The runner invokes this on every
    transition (passed in via ``transition_observer``) so a hook reject
    or hook-with-warnings outcome lands in the same observability sinks
    as agent verdicts.
    """
    del trans
    task = get_task(workspace.root, state.task_id)
    if task is None:
        return
    if isinstance(event, HookOk) and event.warnings:
        _record_hook_warnings(
            workspace,
            task,
            phase=from_stage,
            warnings=event.warnings,
        )
        return
    if isinstance(event, Reject) and event.source == "hook":
        hook = event.metadata.get("hook")
        if isinstance(hook, dict):
            hook_arg = hook
        else:
            hook_arg = None
        consecutive_same_hook_rejects = event.metadata.get("consecutive_same_hook_rejects")
        if isinstance(consecutive_same_hook_rejects, int):
            consecutive_same_hook_rejects_arg = consecutive_same_hook_rejects
        else:
            consecutive_same_hook_rejects_arg = None
        _record_hook_reject(
            workspace,
            task,
            phase=from_stage,
            reason=event.reason,
            warnings=[str(item) for item in event.metadata.get("warnings", [])],
            hook=hook_arg,
            consecutive_same_hook_rejects=consecutive_same_hook_rejects_arg,
        )
