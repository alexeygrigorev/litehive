"""CLI adapter implementations."""

from heru.adapters.claude import ClaudeCLIAdapter
from heru.adapters.codex import CodexCLIAdapter
from heru.adapters.common import (
    EngineError,
    RetryableExecutionFailure,
    _ENGINE_LIMIT_PATTERNS,
    _EXECUTION_INTERRUPTION_PATTERNS,
    _RETRYABLE_EXECUTION_PATTERNS,
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
)
from heru.adapters.copilot import CopilotCLIAdapter
from heru.adapters.gemini import GeminiCLIAdapter
from heru.adapters.goz import GozCLIAdapter
from heru.adapters.opencode import OpenCodeAdapter

__all__ = [
    "ClaudeCLIAdapter",
    "CodexCLIAdapter",
    "CopilotCLIAdapter",
    "EngineError",
    "GeminiCLIAdapter",
    "GozCLIAdapter",
    "OpenCodeAdapter",
    "RetryableExecutionFailure",
    "_ENGINE_LIMIT_PATTERNS",
    "_EXECUTION_INTERRUPTION_PATTERNS",
    "_RETRYABLE_EXECUTION_PATTERNS",
    "classify_execution_interruption",
    "classify_execution_limit",
    "classify_retryable_execution_failure",
]
