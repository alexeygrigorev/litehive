"""Compatibility shim for extracted engine detection helpers."""

from importlib import reload

import heru._engine_detection as _impl

reload(_impl)

_ORIGINAL_EXTERNAL_ADAPTER_RUN = _impl._ORIGINAL_EXTERNAL_ADAPTER_RUN
_ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE = _impl._ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE
_effective_engine_callable = _impl._effective_engine_callable
_filter_supported_kwargs = _impl._filter_supported_kwargs
_has_callable_override = _impl._has_callable_override
_prefers_non_live_run = _impl._prefers_non_live_run
_supports_live_execution = _impl._supports_live_execution
_supports_live_on_started = _impl._supports_live_on_started
_supports_on_started = _impl._supports_on_started

__all__ = [
    "_ORIGINAL_EXTERNAL_ADAPTER_RUN",
    "_ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE",
    "_effective_engine_callable",
    "_filter_supported_kwargs",
    "_has_callable_override",
    "_prefers_non_live_run",
    "_supports_live_execution",
    "_supports_live_on_started",
    "_supports_on_started",
]
