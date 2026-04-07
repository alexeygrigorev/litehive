"""Per-engine adapter classes and helpers."""

from litehive.engines.adapters.claude import (
    ClaudeCLIAdapter,
    _CLAUDE_STREAM_EVENT_ADAPTER,
    _claude_live_events,
    _unwrap_claude_stream_event,
)
from litehive.engines.adapters.codex import CodexCLIAdapter, _codex_live_events
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
from litehive.engines.adapters.copilot import (
    CopilotCLIAdapter,
    _COPILOT_STREAM_EVENT_ADAPTER,
    _copilot_live_events,
)
from litehive.engines.adapters.gemini import GeminiCLIAdapter, _gemini_live_events
from litehive.engines.adapters.goz import GozCLIAdapter, _goz_live_events
from litehive.engines.adapters.opencode import (
    OpenCodeAdapter,
    _OPENCODE_STRIPPED_ENV_VARS,
    _opencode_live_events,
)

__all__ = [
    "ClaudeCLIAdapter",
    "CodexCLIAdapter",
    "CopilotCLIAdapter",
    "EngineError",
    "GeminiCLIAdapter",
    "GozCLIAdapter",
    "OpenCodeAdapter",
    "RetryableExecutionFailure",
    "_CLAUDE_STREAM_EVENT_ADAPTER",
    "_COPILOT_STREAM_EVENT_ADAPTER",
    "_ENGINE_LIMIT_PATTERNS",
    "_EXECUTION_INTERRUPTION_PATTERNS",
    "_OPENCODE_STRIPPED_ENV_VARS",
    "_RETRYABLE_EXECUTION_PATTERNS",
    "_claude_live_events",
    "_codex_live_events",
    "_copilot_live_events",
    "_gemini_live_events",
    "_goz_live_events",
    "_opencode_live_events",
    "_unwrap_claude_stream_event",
    "classify_execution_interruption",
    "classify_execution_limit",
    "classify_retryable_execution_failure",
]
