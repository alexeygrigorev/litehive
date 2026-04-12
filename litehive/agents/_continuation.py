"""Continuation helpers for subagent sessions and runtime handoffs."""

from heru import extract_engine_continuation as _extract_engine_continuation
from heru.base import CLIExecutionResult
from heru.types import RuntimeEngineContinuation

from litehive.agents.unified_events import parse_unified_execution


def extract_engine_continuation(
    engine_name: str,
    execution: CLIExecutionResult | None,
) -> RuntimeEngineContinuation | None:
    """Delegate engine-specific continuation extraction to Heru."""
    return _extract_engine_continuation(engine_name, execution)


def extract_execution_continuation(
    engine_name: str,
    execution: CLIExecutionResult | None,
) -> RuntimeEngineContinuation | None:
    """Prefer Litehive's unified event stream, then fall back to Heru."""
    if execution is None:
        return None
    unified = parse_unified_execution(execution.stdout)
    if unified is not None:
        continuation = unified.continuation()
        if continuation is not None:
            return continuation
    return extract_engine_continuation(engine_name, execution)


__all__ = ["extract_engine_continuation", "extract_execution_continuation"]
