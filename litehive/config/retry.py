"""Execution retry policy defaults with a single shared definition."""

from litehive.config.dataclasses import ExecutionRetryPolicy


_DEFAULT_RETRY_MAX_RETRIES = 2
_DEFAULT_RETRY_BACKOFF_SECONDS = 0.25
_DEFAULT_RETRY_BACKOFF_MULTIPLIER = 2.0
_DEFAULT_RETRY_ON = ("timeout", "network", "service")
_DEFAULT_RETRY_POLICY_ENGINES = ("claude", "codex", "opencode", "gemini", "goz")


def _make_default_retry_policy() -> ExecutionRetryPolicy:
    return ExecutionRetryPolicy(
        max_retries=_DEFAULT_RETRY_MAX_RETRIES,
        backoff_seconds=_DEFAULT_RETRY_BACKOFF_SECONDS,
        backoff_multiplier=_DEFAULT_RETRY_BACKOFF_MULTIPLIER,
        retry_on=list(_DEFAULT_RETRY_ON),
    )


def default_execution_retry_policies() -> dict[str, ExecutionRetryPolicy]:
    return {engine: _make_default_retry_policy() for engine in _DEFAULT_RETRY_POLICY_ENGINES}
