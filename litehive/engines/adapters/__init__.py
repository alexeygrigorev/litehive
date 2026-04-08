"""Per-engine adapter classes and shared error classification helpers."""

from litehive.engines.adapters.claude import ClaudeCLIAdapter
from litehive.engines.adapters.codex import CodexCLIAdapter
from litehive.engines.adapters.common import (
    EngineError,
    RetryableExecutionFailure,
    _ENGINE_LIMIT_PATTERNS,
    _EXECUTION_INTERRUPTION_PATTERNS,
    _RETRYABLE_EXECUTION_PATTERNS,
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
)
from litehive.engines.adapters.copilot import CopilotCLIAdapter
from litehive.engines.adapters.gemini import GeminiCLIAdapter
from litehive.engines.adapters.goz import GozCLIAdapter
from litehive.engines.adapters.opencode import OpenCodeAdapter

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
