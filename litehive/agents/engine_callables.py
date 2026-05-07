"""Typed helpers for resolving engine run callables."""

from collections.abc import Callable
from typing import cast

from heru.base import CLIExecutionResult
from heru.engine_detection import effective_engine_callable


def resolve_cli_execution_callable(engine: object, name: str) -> Callable[..., CLIExecutionResult]:
    """
    Return the effective engine method used to run a CLI invocation.

    Heru's override detector returns ``object | None`` because it
    works against arbitrary adapters. Litehive narrows that boundary
    once here by checking callability and returning the callable shape
    every manager/sandbox call site expects.
    """
    method = effective_engine_callable(engine, name)
    if method is None:
        method = getattr(engine, name, None)
    if not callable(method):
        raise TypeError(f"Engine {type(engine).__name__} has no callable {name}")
    return cast(Callable[..., CLIExecutionResult], method)
