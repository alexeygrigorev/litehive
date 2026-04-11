from dataclasses import dataclass
from typing import Callable

from .events import Event
from .persistence import TaskState
from .types import NodeName, PipelineMode

GuardFn = Callable[[TaskState, Event], bool]


@dataclass(frozen=True)
class Guard:
    """Composable predicate used by transition rules."""

    fn: GuardFn
    description: str = ""

    def __call__(self, state: TaskState, event: Event) -> bool:
        return self.fn(state, event)

    def __and__(self, other: "Guard") -> "Guard":
        left, right = self.fn, other.fn

        def both(state: TaskState, event: Event) -> bool:
            return left(state, event) and right(state, event)

        return Guard(both, f"({self.description} AND {other.description})")

    def __or__(self, other: "Guard") -> "Guard":
        left, right = self.fn, other.fn

        def either(state: TaskState, event: Event) -> bool:
            return left(state, event) or right(state, event)

        return Guard(either, f"({self.description} OR {other.description})")

    def __invert__(self) -> "Guard":
        inner = self.fn

        def negated(state: TaskState, event: Event) -> bool:
            return not inner(state, event)

        return Guard(negated, f"NOT ({self.description})")


def mode(m: PipelineMode | str) -> Guard:
    want = m if isinstance(m, PipelineMode) else PipelineMode(m)

    def check(state: TaskState, event: Event) -> bool:
        return state.pipeline_mode == want

    return Guard(check, f"mode={want.value}")


def stage_retries_remaining(stage: NodeName) -> Guard:
    def check(state: TaskState, event: Event) -> bool:
        return state.stage_retry.get(stage, 0) < state.limits.stage_retry_limit

    return Guard(check, f"stage_retries_remaining({stage})")


def stage_retries_exhausted(stage: NodeName) -> Guard:
    def check(state: TaskState, event: Event) -> bool:
        return state.stage_retry.get(stage, 0) >= state.limits.stage_retry_limit

    return Guard(check, f"stage_retries_exhausted({stage})")


def zero_change_shortcut() -> Guard:
    def check(state: TaskState, event: Event) -> bool:
        return state.last_report.files_changed == 0 and state.last_report.tests_added == 0

    return Guard(check, "zero_change_shortcut")


def pre_exec_budget_remaining() -> Guard:
    def check(state: TaskState, event: Event) -> bool:
        return state.pre_exec_recovery_attempt < 1

    return Guard(check, "pre_exec_budget_remaining")


def _always(state: TaskState, event: Event) -> bool:
    return True


always = Guard(_always, "always")
