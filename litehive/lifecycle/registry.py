"""Factory that assembles a full ``NodeRegistry`` for the state machine.

Takes the injectable dependencies (engine selector, session store, hook
runner, commit node, prompt context, per-phase hook specs) and registers
one instance of every node the transition table can route to:

  - ``ready`` (system probe)
  - ``recovering_pre_exec`` (system probe)
  - 9 hook nodes at the supported stage boundaries
  - 4 stage agent nodes: grooming/implementing/testing/accepting
  - ``commit`` (system)
  - ``recovering`` (recovery agent)
  - ``done`` and ``failed`` (terminals)

Callers wire up their own selector / sessions / hook runner / commit node
and pass them in; this module doesn't import any concrete engine or git
plumbing.
"""

from litehive.roles.base import PromptContext, RoleAgent
from litehive.domain.common import PipelineState
from litehive.roles.merge import MergeAgent
from litehive.roles.planner import PlannerAgent
from litehive.roles.qa import QAAgent
from litehive.roles.recovery import RecoveryAgent
from litehive.roles.reviewer import ReviewerAgent
from litehive.roles.swe import SWEAgent
from .nodes.base import NodeRegistry
from .nodes.hook import HookNode, HookRunner, HookSpec
from .nodes.system import (
    CommitNode,
    NoopWorktreeSyncNode,
    PreExecRecoveryNode,
    ReadyNode,
    WorktreeSyncNode,
)
from .nodes.terminal import TerminalNode
from .nodes.agent import EngineSelector, SessionProvider

HOOK_PHASES: tuple[PipelineState, ...] = (
    PipelineState.BEFORE_GROOMING,
    PipelineState.AFTER_GROOMING,
    PipelineState.BEFORE_IMPLEMENTING,
    PipelineState.AFTER_IMPLEMENTING,
    PipelineState.BEFORE_TESTING,
    PipelineState.AFTER_TESTING,
    PipelineState.BEFORE_ACCEPTING,
    PipelineState.AFTER_ACCEPTING,
    PipelineState.AFTER_COMMIT,
)


def _phase_hook_node(name: PipelineState, hooks: list[HookSpec], runner: HookRunner) -> HookNode:
    return HookNode(name, hooks=hooks, runner=runner)


def build_registry(
    selector: EngineSelector,
    session_store: SessionProvider,
    hook_runner: HookRunner,
    commit_node: CommitNode,
    prompt_context: PromptContext,
    worktree_sync_node: WorktreeSyncNode | None = None,
    ready_node: ReadyNode | None = None,
    pre_exec_recovery_node: PreExecRecoveryNode | None = None,
    hook_specs: dict[PipelineState, list[HookSpec]] | None = None,
    retry_budget: int = 3,
    retry_on: tuple[str, ...] = ("execution_limit", "timeout"),
) -> NodeRegistry:
    """Assemble every node the state machine can route to.

    ``hook_specs`` maps phase name (e.g. ``"before_implementing"``) to a list
    of ``HookSpec``. Missing phases default to an empty list, producing a
    ``HookNode`` that immediately returns ``HookOk``.

    ``commit_node`` must be a ``CommitNode`` subclass whose
    ``_merge_worktree`` is wired to real git (or a stub in tests). It does
    automatic merges only — on conflict it raises ``MergeConflict`` and
    the state machine routes the task to ``merge_resolving`` where the
    ``MergeAgent`` (also registered here) takes one shot at cleanup.
    """
    hook_specs = hook_specs or {}
    registry = NodeRegistry()

    # Entry + pre-exec probes + worktree sync
    registry.register(ready_node or ReadyNode())
    registry.register(pre_exec_recovery_node or PreExecRecoveryNode())
    registry.register(worktree_sync_node or NoopWorktreeSyncNode())

    # 9 supported hook phases
    for phase_name in HOOK_PHASES:
        registry.register(
            _phase_hook_node(
                phase_name,
                hook_specs.get(phase_name, []),
                hook_runner,
            )
        )

    # 4 agent stages
    agent_classes: tuple[type[RoleAgent], ...] = (
        PlannerAgent,
        SWEAgent,
        QAAgent,
        ReviewerAgent,
    )
    for cls in agent_classes:
        registry.register(
            cls(
                selector=selector,
                session_provider=session_store,
                prompt_context=prompt_context,
                retry_budget=retry_budget,
                retry_on=retry_on,
            )
        )

    # Commit (automatic merge) and its conflict-resolver partner
    registry.register(commit_node)
    registry.register(
        MergeAgent(
            selector=selector,
            session_provider=session_store,
            prompt_context=prompt_context,
            retry_budget=retry_budget,
            retry_on=retry_on,
        )
    )
    registry.register(
        RecoveryAgent(
            selector=selector,
            session_provider=session_store,
            prompt_context=prompt_context,
            retry_budget=retry_budget,
            retry_on=retry_on,
        )
    )

    # Terminals
    registry.register(TerminalNode(PipelineState.DONE))
    registry.register(TerminalNode(PipelineState.FAILED))

    return registry
