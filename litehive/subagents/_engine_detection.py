"""Engine capability detection helpers."""

import inspect

from litehive.engines.base import ExternalCLIAdapter

_ORIGINAL_EXTERNAL_ADAPTER_RUN = ExternalCLIAdapter.run
_ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE = ExternalCLIAdapter.run_live
_SEEN_INHERITED_CALLABLES: dict[str, set[object]] = {
    "run": {_ORIGINAL_EXTERNAL_ADAPTER_RUN},
    "run_live": {_ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE},
}


def _supports_live_execution(engine: object) -> bool:
    run_live = getattr(engine, "run_live", None)
    if not callable(run_live):
        return False
    return not _prefers_non_live_run(engine)


def _unwrap_bound_callable(method: object) -> object:
    return getattr(method, "__func__", method)


def _is_instance_alias_to_inherited_callable(engine: object, name: str, value: object) -> bool:
    if not callable(value):
        return False
    if getattr(value, "__self__", None) is not engine:
        return False
    resolved = _unwrap_bound_callable(value)
    seen = _SEEN_INHERITED_CALLABLES.setdefault(name, set())
    for cls in type(engine).__mro__:
        inherited = cls.__dict__.get(name)
        if callable(inherited) and _unwrap_bound_callable(inherited) is resolved:
            seen.add(resolved)
            return True
    return resolved in seen


def _has_callable_override(engine: object, name: str, default: object) -> bool:
    method = getattr(engine, name, None)
    if not callable(method):
        return False
    resolved = _unwrap_bound_callable(method)
    instance_dict = getattr(engine, "__dict__", None)
    if isinstance(instance_dict, dict) and name in instance_dict:
        value = instance_dict[name]
        if callable(value):
            # Ignore instance-level aliases to bound methods. Tests and monkeypatch
            # cleanup can leave behind a rebound inherited method in __dict__, and
            # that should not outrank an available inherited run_live method.
            if _is_instance_alias_to_inherited_callable(engine, name, value):
                return False
            rebound = _unwrap_bound_callable(value)
            for cls in type(engine).__mro__:
                inherited = cls.__dict__.get(name)
                if callable(inherited) and _unwrap_bound_callable(inherited) is rebound:
                    return False
    if name == "run" and resolved is _ORIGINAL_EXTERNAL_ADAPTER_RUN:
        return False
    if name == "run_live" and resolved is _ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE:
        return False
    instance_dict = getattr(engine, "__dict__", None)
    if isinstance(instance_dict, dict) and name in instance_dict:
        value = instance_dict[name]
        if callable(value):
            rebound = _unwrap_bound_callable(value)
            for cls in type(engine).__mro__:
                inherited = cls.__dict__.get(name)
                if callable(inherited) and _unwrap_bound_callable(inherited) is rebound:
                    return False
    return resolved is not default


def _callable_resolution_rank(engine: object, name: str) -> int | None:
    target = getattr(engine, name, None)
    if not callable(target):
        return None
    instance_dict = getattr(engine, "__dict__", None)
    if isinstance(instance_dict, dict) and name in instance_dict:
        value = instance_dict[name]
        if callable(value):
            if _is_instance_alias_to_inherited_callable(engine, name, value):
                return None
            resolved = _unwrap_bound_callable(value)
            for index, cls in enumerate(type(engine).__mro__):
                inherited = cls.__dict__.get(name)
                if callable(inherited) and _unwrap_bound_callable(inherited) is resolved:
                    return None
            return -1
    for index, cls in enumerate(type(engine).__mro__):
        value = cls.__dict__.get(name)
        if callable(value):
            resolved = _unwrap_bound_callable(value)
            _SEEN_INHERITED_CALLABLES.setdefault(name, set()).add(resolved)
            for parent_index, parent in enumerate(type(engine).__mro__[index + 1 :], start=index + 1):
                inherited = parent.__dict__.get(name)
                if callable(inherited) and _unwrap_bound_callable(inherited) is resolved:
                    _SEEN_INHERITED_CALLABLES.setdefault(name, set()).add(resolved)
                    return parent_index
            return index
    return None


def _prefers_non_live_run(engine: object) -> bool:
    if not _has_callable_override(engine, "run", _ORIGINAL_EXTERNAL_ADAPTER_RUN):
        return False
    if not _has_callable_override(engine, "run_live", _ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE):
        return True
    run_rank = _callable_resolution_rank(engine, "run")
    run_live_rank = _callable_resolution_rank(engine, "run_live")
    if run_rank is None:
        return False
    if run_live_rank is None:
        return True
    return run_rank < run_live_rank


def _supports_on_started(engine: object) -> bool:
    run = getattr(engine, "run", None)
    if not callable(run):
        return False
    try:
        return "on_started" in inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False


def _supports_live_on_started(engine: object) -> bool:
    run_live = getattr(engine, "run_live", None)
    if not callable(run_live):
        return False
    try:
        return "on_started" in inspect.signature(run_live).parameters
    except (TypeError, ValueError):
        return False
