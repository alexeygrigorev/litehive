"""Compatibility shim for extracted engine detection helpers."""

from importlib import reload

import heru._engine_detection as _impl

reload(_impl)

ORIGINAL_EXTERNAL_ADAPTER_RUN = _impl._ORIGINAL_EXTERNAL_ADAPTER_RUN
ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE = _impl._ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE
effective_engine_callable = _impl._effective_engine_callable
filter_supported_kwargs = _impl._filter_supported_kwargs
has_callable_override = _impl._has_callable_override
prefers_non_live_run = _impl._prefers_non_live_run
supports_live_execution = _impl._supports_live_execution
supports_live_on_started = _impl._supports_live_on_started
supports_on_started = _impl._supports_on_started

__all__ = [
    "ORIGINAL_EXTERNAL_ADAPTER_RUN",
    "ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE",
    "effective_engine_callable",
    "filter_supported_kwargs",
    "has_callable_override",
    "prefers_non_live_run",
    "supports_live_execution",
    "supports_live_on_started",
    "supports_on_started",
]
