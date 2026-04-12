"""Pipeline stages — the enum of states the machine can be in.

Each stage links to the Node class that executes there.
Ctrl+click: ``Stages.GROOMING`` → here → ``node=PlannerAgent`` → the agent code.
"""

from dataclasses import dataclass

from .agents.merge import MergeAgent
from .agents.planner import PlannerAgent
from .agents.qa import QAAgent
from .agents.recovery import RecoveryAgent
from .agents.reviewer import ReviewerAgent
from .agents.swe import SWEAgent
from .nodes.hook import HookNode
from .nodes.system import (
    CommitNode,
    PreExecRecoveryNode,
    ReadyNode,
    WorktreeSyncNode,
)
from .nodes.terminal import TerminalNode


@dataclass(frozen=True)
class Stage:
    name: str
    node: type

    def __eq__(self, other):
        if isinstance(other, str):
            return self.name == other
        if isinstance(other, Stage):
            return self.name == other.name
        return NotImplemented

    def __hash__(self):
        return hash(self.name)

    def __repr__(self):
        return self.name


class Stages:
    """All pipeline stages. Use ``Stages.GROOMING`` in rules, ctrl+click to see the node."""

    # entry
    READY               = Stage("ready",               ReadyNode)
    WORKTREE_SYNC       = Stage("worktree_sync",       WorktreeSyncNode)
    PRE_EXEC_RECOVERY            = Stage("recovering_pre_exec", PreExecRecoveryNode)

    # grooming
    BEFORE_GROOMING     = Stage("before_grooming",     HookNode)
    GROOMING            = Stage("grooming",            PlannerAgent)
    AFTER_GROOMING      = Stage("after_grooming",      HookNode)

    # implementing
    BEFORE_IMPLEMENTING = Stage("before_implementing", HookNode)
    IMPLEMENTING        = Stage("implementing",        SWEAgent)
    AFTER_IMPLEMENTING  = Stage("after_implementing",  HookNode)

    # testing
    BEFORE_TESTING      = Stage("before_testing",      HookNode)
    TESTING             = Stage("testing",             QAAgent)
    AFTER_TESTING       = Stage("after_testing",       HookNode)

    # accepting
    BEFORE_ACCEPTING    = Stage("before_accepting",    HookNode)
    ACCEPTING           = Stage("accepting",           ReviewerAgent)
    AFTER_ACCEPTING     = Stage("after_accepting",     HookNode)

    # commit
    BEFORE_COMMIT       = Stage("before_commit",       HookNode)
    COMMIT              = Stage("commit",              CommitNode)
    AFTER_COMMIT        = Stage("after_commit",        HookNode)
    MERGE_RESOLVING     = Stage("merge_resolving",     MergeAgent)

    # recovery + terminals
    RECOVERING          = Stage("recovering",          RecoveryAgent)
    DONE                = Stage("done",                TerminalNode)
    FAILED              = Stage("failed",              TerminalNode)

    # wildcard sets
    ALL_STAGE_PHASES = frozenset({
        BEFORE_GROOMING, GROOMING, AFTER_GROOMING,
        BEFORE_IMPLEMENTING, IMPLEMENTING, AFTER_IMPLEMENTING,
        BEFORE_TESTING, TESTING, AFTER_TESTING,
        BEFORE_ACCEPTING, ACCEPTING, AFTER_ACCEPTING,
        BEFORE_COMMIT, COMMIT, AFTER_COMMIT,
    })

    GROOMING_EPOCH      = (BEFORE_GROOMING, GROOMING, AFTER_GROOMING)
    IMPLEMENTING_EPOCH  = (BEFORE_IMPLEMENTING, IMPLEMENTING, AFTER_IMPLEMENTING)
    TESTING_EPOCH       = (BEFORE_TESTING, TESTING, AFTER_TESTING)
    ACCEPTING_EPOCH     = (BEFORE_ACCEPTING, ACCEPTING, AFTER_ACCEPTING)
    COMMIT_EPOCH        = (BEFORE_COMMIT, COMMIT, AFTER_COMMIT)
