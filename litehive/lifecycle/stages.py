"""Pipeline stages — the enum of states the machine can be in.

Each stage links to the Node class that executes there.
Ctrl+click: ``Stages.GROOMING`` → here → ``node=PlannerAgent`` → the agent code.
"""

from dataclasses import dataclass

from litehive.domain.common import PipelineState, canonical_pipeline_state
from litehive.roles.merge import MergeAgent
from litehive.roles.planner import PlannerAgent
from litehive.roles.qa import QAAgent
from litehive.roles.recovery import RecoveryAgent
from litehive.roles.reviewer import ReviewerAgent
from litehive.roles.swe import SWEAgent
from .nodes.hook import HookNode
from .nodes.system import CommitNode, PreExecRecoveryNode, ReadyNode, WorktreeSyncNode
from .nodes.terminal import TerminalNode


@dataclass(frozen=True)
class Stage:
    name: PipelineState
    node: type

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", canonical_pipeline_state(self.name))

    def __eq__(self, other):
        if isinstance(other, str):
            return self.name == other
        if isinstance(other, Stage):
            return self.name == other.name
        return NotImplemented

    def __hash__(self):
        return hash(self.name)

    def __lt__(self, other):
        if isinstance(other, Stage):
            return self.name < other.name
        if isinstance(other, str):
            return self.name < other
        return NotImplemented

    def __repr__(self):
        return str(self.name)


class Stages:
    # entry
    READY = Stage(PipelineState.READY, ReadyNode)
    WORKTREE_SYNC = Stage(PipelineState.WORKTREE_SYNC, WorktreeSyncNode)
    PRE_EXEC_RECOVERY = Stage(PipelineState.RECOVERING_PRE_EXEC, PreExecRecoveryNode)

    # grooming
    BEFORE_GROOMING = Stage(PipelineState.BEFORE_GROOMING, HookNode)
    GROOMING = Stage(PipelineState.GROOMING, PlannerAgent)
    AFTER_GROOMING = Stage(PipelineState.AFTER_GROOMING, HookNode)

    # implementing
    BEFORE_IMPLEMENTING = Stage(PipelineState.BEFORE_IMPLEMENTING, HookNode)
    IMPLEMENTING = Stage(PipelineState.IMPLEMENTING, SWEAgent)
    AFTER_IMPLEMENTING = Stage(PipelineState.AFTER_IMPLEMENTING, HookNode)

    # testing
    BEFORE_TESTING = Stage(PipelineState.BEFORE_TESTING, HookNode)
    TESTING = Stage(PipelineState.TESTING, QAAgent)
    AFTER_TESTING = Stage(PipelineState.AFTER_TESTING, HookNode)

    # accepting
    BEFORE_ACCEPTING = Stage(PipelineState.BEFORE_ACCEPTING, HookNode)
    ACCEPTING = Stage(PipelineState.ACCEPTING, ReviewerAgent)
    AFTER_ACCEPTING = Stage(PipelineState.AFTER_ACCEPTING, HookNode)

    # commit
    COMMIT = Stage(PipelineState.COMMIT, CommitNode)
    AFTER_COMMIT = Stage(PipelineState.AFTER_COMMIT, HookNode)
    MERGE_RESOLVING = Stage(PipelineState.MERGE_RESOLVING, MergeAgent)

    # recovery + terminals
    RECOVERING = Stage(PipelineState.RECOVERING, RecoveryAgent)
    DONE = Stage(PipelineState.DONE, TerminalNode)
    FAILED = Stage(PipelineState.FAILED, TerminalNode)

    # wildcard sets
    ALL_STAGE_PHASES = frozenset(
        {
            BEFORE_GROOMING,
            GROOMING,
            AFTER_GROOMING,
            BEFORE_IMPLEMENTING,
            IMPLEMENTING,
            AFTER_IMPLEMENTING,
            BEFORE_TESTING,
            TESTING,
            AFTER_TESTING,
            BEFORE_ACCEPTING,
            ACCEPTING,
            AFTER_ACCEPTING,
            COMMIT,
            AFTER_COMMIT,
        }
    )

    GROOMING_EPOCH = (BEFORE_GROOMING, GROOMING, AFTER_GROOMING)
    IMPLEMENTING_EPOCH = (BEFORE_IMPLEMENTING, IMPLEMENTING, AFTER_IMPLEMENTING)
    TESTING_EPOCH = (BEFORE_TESTING, TESTING, AFTER_TESTING)
    ACCEPTING_EPOCH = (BEFORE_ACCEPTING, ACCEPTING, AFTER_ACCEPTING)
    COMMIT_EPOCH = (COMMIT, AFTER_COMMIT)
