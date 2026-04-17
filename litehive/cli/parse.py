try:
    from heru import ENGINE_CHOICES
except ModuleNotFoundError:  # pragma: no cover - exercised in heru-less workspaces
    ENGINE_CHOICES = ("claude", "codex", "copilot", "gemini", "goz", "opencode")
from litehive.tasks.constants import VALID_TASK_TYPES
from litehive.tasks.normalization import (
    normalize_acceptance_criteria,
    normalize_task_text_list,
)

TASK_TYPE_CHOICES = sorted(VALID_TASK_TYPES)


def parse_dependency_ids(
    raw_values,
    *,
    task_id=None,
    allow_clear=False,
):
    if not raw_values:
        return ...

    dependency_ids = []
    for raw_value in raw_values:
        for item in raw_value.split(","):
            dependency_id = item.strip()
            if not dependency_id:
                raise ValueError("Dependency ids must not be empty")
            dependency_ids.append(dependency_id)

    if allow_clear and len(dependency_ids) == 1 and dependency_ids[0].lower() == "none":
        return []

    normalized = []
    seen = set()
    for dependency_id in dependency_ids:
        if dependency_id.lower() == "none":
            raise ValueError("'none' can only be used by itself")
        if task_id is not None and dependency_id == task_id:
            raise ValueError(f"Task {task_id} cannot depend on itself")
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        normalized.append(dependency_id)
    return normalized


def parse_engine_int_map(raw_values, *, option_name):
    if not raw_values:
        return {}

    mapping = {}
    for raw_value in raw_values:
        engine_name, separator, raw_int = raw_value.partition("=")
        if separator != "=":
            raise ValueError(f"{option_name} entries must use ENGINE=VALUE")
        engine_name = engine_name.strip()
        raw_int = raw_int.strip()
        if engine_name not in ENGINE_CHOICES:
            raise ValueError(f"{option_name} engine must be one of: {', '.join(ENGINE_CHOICES)}")
        try:
            value = int(raw_int)
        except ValueError as exc:
            raise ValueError(f"{option_name} value for {engine_name} must be an integer") from exc
        if value < 0:
            raise ValueError(f"{option_name} value for {engine_name} must be 0 or greater")
        mapping[engine_name] = value
    return mapping


def parse_runner_hooks(
    raw_values,
    *,
    option_name,
):
    if not raw_values:
        return {}

    hooks = {}
    for raw_value in raw_values:
        point, separator, remainder = raw_value.partition("=")
        if separator != "=":
            raise ValueError(f"{option_name} entries must use HOOK_POINT=reject|run:COMMAND")
        mode_label, separator, command = remainder.partition(":")
        if separator != ":":
            raise ValueError(f"{option_name} entries must use HOOK_POINT=reject|run:COMMAND")
        mode_key = mode_label.strip().lower()
        if mode_key not in {"reject", "run"}:
            raise ValueError(f"{option_name} mode must be `reject` or `run`")
        hooks.setdefault(point.strip(), []).append(
            {
                "command": command.strip(),
                "reject_on_failure": mode_key == "reject",
            }
        )
    return hooks


def parse_acceptance_criteria(
    raw_values,
    *,
    allow_clear=False,
):
    if not raw_values:
        return ...

    normalized = normalize_acceptance_criteria(raw_values)
    if allow_clear and len(normalized) == 1 and normalized[0].lower() == "none":
        return []
    if any(item.lower() == "none" for item in normalized):
        raise ValueError("'none' can only be used by itself")
    if not normalized:
        raise ValueError("Acceptance criteria must not be empty")
    return normalized


def parse_text_list_option(
    raw_values,
    *,
    option_name,
    allow_clear=False,
):
    if not raw_values:
        return ...

    normalized = normalize_task_text_list(raw_values)
    if allow_clear and len(normalized) == 1 and normalized[0].lower() == "none":
        return []
    if any(item.lower() == "none" for item in normalized):
        raise ValueError("'none' can only be used by itself")
    if not normalized:
        raise ValueError(f"{option_name} must not be empty")
    return normalized
