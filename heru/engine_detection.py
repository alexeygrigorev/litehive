"""Engine capability detection helpers."""

import inspect

from heru.base import ExternalCLIAdapter

ORIGINAL_EXTERNAL_ADAPTER_RUN = ExternalCLIAdapter.run
ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE = ExternalCLIAdapter.run_live


def supports_live_execution(engine: object) -> bool:
    run_live = getattr(engine, "run_live", None)
    if not callable(run_live):
        return False
    return not prefers_non_live_run(engine)


def _unwrap_bound_callable(method: object) -> object:
    return getattr(method, "__func__", method)


def _looks_like_bound_method_override(method: object) -> bool:
    func = getattr(method, "__func__", None)
    if func is None:
        return True
    try:
        parameters = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return True
    if not parameters:
        return False
    first = parameters[0]
    return first.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def _is_instance_alias_to_inherited_callable(engine: object, name: str, value: object) -> bool:
    if not callable(value):
        return False
    if getattr(value, "__self__", None) is not engine:
        return False
    resolved = _unwrap_bound_callable(value)
    for cls in type(engine).__mro__:
        inherited = cls.__dict__.get(name)
        if callable(inherited) and _unwrap_bound_callable(inherited) is resolved:
            return True
    return False


def _default_callable_for(name: str) -> object | None:
    if name == "run":
        return ORIGINAL_EXTERNAL_ADAPTER_RUN
    if name == "run_live":
        return ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE
    return None


def _current_external_adapter_callable_for(name: str) -> object | None:
    value = ExternalCLIAdapter.__dict__.get(name)
    if not callable(value):
        return None
    return _unwrap_bound_callable(value)


def _current_class_callable_for(engine: object, name: str) -> object | None:
    value = getattr(type(engine), name, None)
    if not callable(value):
        return None
    return _unwrap_bound_callable(value)


def _resolve_inherited_callable_rank(engine: object, name: str, resolved: object) -> int | None:
    for index, cls in enumerate(type(engine).__mro__):
        inherited = cls.__dict__.get(name)
        if callable(inherited) and _unwrap_bound_callable(inherited) is resolved:
            default = _default_callable_for(name)
            current_external = _current_external_adapter_callable_for(name)
            if cls is not ExternalCLIAdapter and (
                resolved is default or resolved is current_external
            ):
                return None
            return index
    return None


def has_callable_override(engine: object, name: str, default: object) -> bool:
    method = getattr(engine, name, None)
    if not callable(method):
        return False
    resolved = _unwrap_bound_callable(method)
    current_class = _current_class_callable_for(engine, name)
    instance_dict = getattr(engine, "__dict__", None)
    if isinstance(instance_dict, dict) and name in instance_dict:
        value = instance_dict[name]
        if callable(value):
            # Ignore instance-level aliases to bound methods. Tests and monkeypatch
            # cleanup can leave behind a rebound inherited method in __dict__, and
            # that should not outrank an available inherited run_live method.
            if _is_instance_alias_to_inherited_callable(engine, name, value):
                rebound = _unwrap_bound_callable(value)
                if (
                    rebound is default
                    and current_class is not None
                    and current_class is not default
                ):
                    return True
                inherited_rank = _resolve_inherited_callable_rank(engine, name, resolved)
                return inherited_rank is not None and resolved is not default
            if getattr(value, "__self__", None) is engine and not _looks_like_bound_method_override(value):
                return False
            rebound = _unwrap_bound_callable(value)
            if (
                rebound is default
                and current_class is not None
                and current_class is not default
            ):
                return True
            if _resolve_inherited_callable_rank(engine, name, rebound) is not None:
                return False
    inherited_rank = _resolve_inherited_callable_rank(engine, name, resolved)
    if inherited_rank is not None:
        return resolved is not default
    instance_dict = getattr(engine, "__dict__", None)
    if (
        current_class is not None
        and current_class is not default
        and resolved is default
        and isinstance(instance_dict, dict)
        and name in instance_dict
    ):
        return True
    if name == "run" and resolved is ORIGINAL_EXTERNAL_ADAPTER_RUN:
        return False
    if name == "run_live" and resolved is ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE:
        return False
    if isinstance(instance_dict, dict) and name in instance_dict:
        value = instance_dict[name]
        if callable(value):
            rebound = _unwrap_bound_callable(value)
            if _resolve_inherited_callable_rank(engine, name, rebound) is not None:
                return False
    return resolved is not default


def _callable_resolution_rank(engine: object, name: str) -> int | None:
    target = getattr(engine, name, None)
    if not callable(target):
        return None

    default = _default_callable_for(name)
    current_class = _current_class_callable_for(engine, name)
    instance_dict = getattr(engine, "__dict__", None)
    if isinstance(instance_dict, dict) and name in instance_dict:
        value = instance_dict[name]
        if callable(value):
            if _is_instance_alias_to_inherited_callable(engine, name, value):
                resolved = _unwrap_bound_callable(value)
                if (
                    current_class is not None
                    and default is not None
                    and resolved is default
                    and current_class is not default
                ):
                    inherited_rank = _resolve_inherited_callable_rank(engine, name, current_class)
                    if inherited_rank is not None:
                        return inherited_rank
                return _resolve_inherited_callable_rank(engine, name, resolved)
            if getattr(value, "__self__", None) is engine and not _looks_like_bound_method_override(value):
                return None
            resolved = _unwrap_bound_callable(value)
            if (
                current_class is not None
                and default is not None
                and resolved is default
                and current_class is not default
            ):
                inherited_rank = _resolve_inherited_callable_rank(engine, name, current_class)
                if inherited_rank is not None:
                    return inherited_rank
            if _resolve_inherited_callable_rank(engine, name, resolved) is not None:
                return None
            return -1
    for index, cls in enumerate(type(engine).__mro__):
        value = cls.__dict__.get(name)
        if callable(value):
            resolved = _unwrap_bound_callable(value)
            if cls is not ExternalCLIAdapter and resolved is default:
                return None
            if index == 0:
                inherited_rank = _resolve_inherited_callable_rank(engine, name, resolved)
                if inherited_rank is not None:
                    return inherited_rank
            for parent_index, parent in enumerate(type(engine).__mro__[index + 1 :], start=index + 1):
                inherited = parent.__dict__.get(name)
                if callable(inherited) and _unwrap_bound_callable(inherited) is resolved:
                    return _resolve_inherited_callable_rank(engine, name, resolved)
            return index
    return None


def prefers_non_live_run(engine: object) -> bool:
    if not has_callable_override(engine, "run", ORIGINAL_EXTERNAL_ADAPTER_RUN):
        return False
    run_rank = _callable_resolution_rank(engine, "run")
    run_live_rank = _callable_resolution_rank(engine, "run_live")
    if run_rank is None:
        return False
    if run_live_rank is None:
        return True
    return run_rank < run_live_rank


def supports_on_started(engine: object) -> bool:
    return _supports_callable_kwarg(getattr(engine, "run", None), "on_started")


def supports_live_on_started(engine: object) -> bool:
    return _supports_callable_kwarg(getattr(engine, "run_live", None), "on_started")


def _supports_callable_kwarg(method: object, name: str) -> bool:
    if not callable(method):
        return False
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    if name in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def filter_supported_kwargs(method: object, kwargs: dict[str, object]) -> dict[str, object]:
    if not callable(method) or not kwargs:
        return kwargs
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return kwargs
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return kwargs
    supported = set(signature.parameters)
    return {key: value for key, value in kwargs.items() if key in supported}


def effective_engine_callable(engine: object, name: str) -> object | None:
    method = getattr(engine, name, None)
    if not callable(method):
        return None
    instance_dict = getattr(engine, "__dict__", None)
    if not isinstance(instance_dict, dict) or name not in instance_dict:
        return method
    value = instance_dict[name]
    if not callable(value):
        return method
    default = _default_callable_for(name)
    current_class = _current_class_callable_for(engine, name)
    rebound = _unwrap_bound_callable(value)
    if (
        default is not None
        and rebound is default
        and current_class is not None
        and current_class is not default
    ):
        descriptor = getattr(type(engine), name, None)
        if descriptor is None:
            return method
        binder = getattr(descriptor, "__get__", None)
        if callable(binder):
            return binder(engine, type(engine))
    return method
