from litehive.tasks.normalization import (
    normalize_acceptance_criteria,
    normalize_task_text_list,
)


def parse_dependency_ids(
    raw_values,
    task_id=None,
    allow_clear=False,
):
    """
    Normalize ``--depends-on`` arguments into a deduplicated id list.

    Splits comma-separated values, strips whitespace, rejects the
    self-edge ``task_id -> task_id``, and de-dupes while preserving
    order. ``allow_clear`` lets ``task update`` accept the literal
    ``none`` to wipe dependencies; plain ``task add`` rejects that
    sentinel because there is nothing to clear yet.

    Returns the sentinel ``...`` when the operator did not pass the
    option so the caller can distinguish "leave existing list alone"
    from "set to empty list".
    """
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


def parse_acceptance_criteria(
    raw_values,
    allow_clear=False,
):
    """
    Normalize repeated ``--acceptance-criteria`` values into a non-empty list.

    ``allow_clear`` lets ``task update`` accept the literal ``none``
    to wipe criteria entirely. Returns the sentinel ``...`` when the
    option was absent so callers can distinguish "unchanged" from
    "set to empty list" — a distinction the SQLite-backed update
    path needs to avoid stomping on existing criteria.
    """
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
    option_name,
    allow_clear=False,
):
    """
    Generic parser for repeatable text-list options (constraints, plan, …).

    Shares the ``none``-clears-everything semantics with
    :func:`parse_acceptance_criteria` so every mutable list field on
    a task behaves the same way at the CLI surface. ``option_name``
    is woven into the error message so the operator sees *which*
    list rejected an empty value.
    """
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
