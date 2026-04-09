"""CLI adapter implementations."""

from litehive.agents.adapters.claude import ClaudeCLIAdapter
from litehive.agents.adapters.codex import CodexCLIAdapter
from litehive.agents.adapters.common import (
    EngineError,
    RetryableExecutionFailure,
    _ENGINE_LIMIT_PATTERNS,
    _EXECUTION_INTERRUPTION_PATTERNS,
    _RETRYABLE_EXECUTION_PATTERNS,
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
)
from litehive.agents.adapters.copilot import CopilotCLIAdapter
from litehive.agents.adapters.gemini import GeminiCLIAdapter
from litehive.agents.adapters.goz import GozCLIAdapter
from litehive.agents.adapters.opencode import OpenCodeAdapter

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
