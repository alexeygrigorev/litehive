"""Factory that assembles a full ``NodeRegistry`` for the v2 state machine.

Takes the injectable dependencies (engine selector, session store, hook
runner, commit node, prompt context, per-phase hook specs) and registers
one instance of every node the transition table can route to:

  - ``ready`` (system probe)
  - ``recovering_pre_exec`` (system probe)
  - 10 hook nodes: ``before_/after_`` × ``grooming/implementing/testing/
    accepting/commit``
  - 4 stage agent nodes: grooming/implementing/testing/accepting
  - ``commit`` (system)
  - ``recovering`` (recovery agent)
  - ``done`` and ``failed`` (terminals)

Callers wire up their own selector / sessions / hook runner / commit node
and pass them in; this module doesn't import any concrete engine or git
plumbing.
"""

from .agents import (
    MergeAgent,
    PlannerAgent,
    QAAgent,
    RecoveryAgent,
    ReviewerAgent,
    SWEAgent,
)
from .agents._base import PromptContext, RoleAgent
from .nodes import (
    CommitNode,
    ExecutionMode,
    HookNode,
    HookRunner,
    HookSpec,
    NodeRegistry,
    NoopWorktreeSyncNode,
    PreExecRecoveryNode,
    ReadyNode,
    TerminalNode,
    WorktreeSyncNode,
)
from .nodes.agent import EngineSelector, SessionProvider
from .types import NodeName

PHASES: tuple[tuple[NodeName, NodeName], ...] = (
    ("before_grooming", "after_grooming"),
    ("before_implementing", "after_implementing"),
    ("before_testing", "after_testing"),
    ("before_accepting", "after_accepting"),
    ("before_commit", "after_commit"),
)


def _phase_hook_node(
    name: NodeName,
    hooks: list[HookSpec],
    runner: HookRunner,
    execution_mode: ExecutionMode,
) -> HookNode:
    return HookNode(name, hooks=hooks, runner=runner, execution_mode=execution_mode)


def build_registry(
    *,
    selector: EngineSelector,
    session_store: SessionProvider,
    hook_runner: HookRunner,
    commit_node: CommitNode,
    worktree_sync_node: WorktreeSyncNode | None = None,
    ready_node: ReadyNode | None = None,
    pre_exec_recovery_node: PreExecRecoveryNode | None = None,
    prompt_context: PromptContext | None = None,
    hook_specs: dict[NodeName, list[HookSpec]] | None = None,
    hook_execution_mode: ExecutionMode = ExecutionMode.FAIL_FAST,
    retry_budget: int = 3,
    retry_backoff_seconds: float = 0.0,
    retry_backoff_multiplier: float = 2.0,
) -> NodeRegistry:
    """Assemble every node the v2 state machine can route to.

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

    # 10 hook phases (before/after × 5 stages)
    for before_name, after_name in PHASES:
        registry.register(
            _phase_hook_node(
                before_name,
                hook_specs.get(before_name, []),
                hook_runner,
                hook_execution_mode,
            )
        )
        registry.register(
            _phase_hook_node(
                after_name,
                hook_specs.get(after_name, []),
                hook_runner,
                hook_execution_mode,
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
        )
    )
    registry.register(
        RecoveryAgent(
            selector=selector,
            session_provider=session_store,
            prompt_context=prompt_context,
            retry_budget=retry_budget,
        )
    )

    # Terminals
    registry.register(TerminalNode("done"))
    registry.register(TerminalNode("failed"))

    return registry
