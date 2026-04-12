import os
import shlex
import subprocess
import tempfile

import yaml

from litehive.agents import ENGINE_CHOICES
from litehive.tasks.constants import (
    VALID_PM_COMPLEXITIES,
    VALID_PLANNED_EFFORTS,
    VALID_TASK_PRIORITIES,
    VALID_TASK_TYPES,
)
from litehive.tasks.crud import require_task
from litehive.tasks.normalization import (
    normalize_acceptance_criteria,
    normalize_human_checkpoints,
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
            raise ValueError(
                f"{option_name} entries must use HOOK_POINT=reject|run:COMMAND"
            )
        mode_label, separator, command = remainder.partition(":")
        if separator != ":":
            raise ValueError(
                f"{option_name} entries must use HOOK_POINT=reject|run:COMMAND"
            )
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


_RICH_TASK_UPDATE_KEYS = {
    "title",
    "goal",
    "acceptance_criteria",
    "constraints",
    "plan",
    "pm_complexity",
    "planned_effort",
    "depends_on",
    "human_checkpoints",
    "task_type",
    "mode",
    "priority",
    "model",
    "retry_limit",
    "auto_commit",
    "outcome",
    "outcome_reason",
    "action",
}

_VALID_CLOSE_OUTCOMES = {"wont_do", "deferred", "duplicate"}
_VALID_ACTIONS = {"park", "requeue", "abandon"}


def _normalize_yaml_text_list(value, *, key):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a YAML list of strings")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a YAML list of strings")
    return normalize_task_text_list(value)


def parse_rich_task_update_document(data, *, source):
    if data is None:
        raise ValueError(f"{source} is empty")
    if not isinstance(data, dict):
        raise ValueError(f"{source} must contain a YAML mapping of task fields")

    unknown = sorted(key for key in data if key not in _RICH_TASK_UPDATE_KEYS)
    if unknown:
        allowed = ", ".join(sorted(_RICH_TASK_UPDATE_KEYS))
        raise ValueError(
            f"{source} contains unsupported field(s): {', '.join(unknown)}. "
            f"Supported fields: {allowed}"
        )

    updates = {}

    if "title" in data:
        title = data["title"]
        if not isinstance(title, str):
            raise ValueError(f"{source} field 'title' must be a string")
        updates["title"] = title.strip()

    if "goal" in data:
        goal = data["goal"]
        if goal is None:
            updates["goal"] = ""
        elif isinstance(goal, str):
            updates["goal"] = goal.strip()
        else:
            raise ValueError(f"{source} field 'goal' must be a string or null")

    if "acceptance_criteria" in data:
        updates["acceptance_criteria"] = normalize_acceptance_criteria(
            _normalize_yaml_text_list(data["acceptance_criteria"], key="acceptance_criteria")
        )

    if "constraints" in data:
        updates["constraints"] = _normalize_yaml_text_list(data["constraints"], key="constraints")

    if "plan" in data:
        updates["plan"] = _normalize_yaml_text_list(data["plan"], key="plan")

    if "pm_complexity" in data:
        pm_complexity = data["pm_complexity"]
        if pm_complexity is not None and pm_complexity not in VALID_PM_COMPLEXITIES:
            allowed = ", ".join(sorted(VALID_PM_COMPLEXITIES))
            raise ValueError(f"{source} field 'pm_complexity' must be one of: {allowed}, or null")
        updates["pm_complexity"] = pm_complexity

    if "planned_effort" in data:
        planned_effort = data["planned_effort"]
        if planned_effort is not None and planned_effort not in VALID_PLANNED_EFFORTS:
            allowed = ", ".join(sorted(VALID_PLANNED_EFFORTS))
            raise ValueError(f"{source} field 'planned_effort' must be one of: {allowed}, or null")
        updates["planned_effort"] = planned_effort

    if "depends_on" in data:
        depends_on = data["depends_on"]
        if depends_on is None:
            updates["depends_on"] = []
        else:
            if not isinstance(depends_on, list) or any(
                not isinstance(item, str) for item in depends_on
            ):
                raise ValueError(f"{source} field 'depends_on' must be a YAML list of task ids")
            updates["depends_on"] = depends_on

    if "human_checkpoints" in data:
        checkpoints = data["human_checkpoints"]
        if checkpoints is None:
            updates["human_checkpoints"] = []
        else:
            if not isinstance(checkpoints, list) or any(
                not isinstance(item, str) for item in checkpoints
            ):
                raise ValueError(
                    f"{source} field 'human_checkpoints' must be a YAML list of strings"
                )
            updates["human_checkpoints"] = normalize_human_checkpoints(checkpoints)

    if "task_type" in data:
        task_type = data["task_type"]
        if task_type is not None and task_type not in TASK_TYPE_CHOICES:
            allowed = ", ".join(TASK_TYPE_CHOICES)
            raise ValueError(f"{source} field 'task_type' must be one of: {allowed}, or null")
        updates["task_type"] = task_type

    if "mode" in data:
        mode = data["mode"]
        if mode not in {"tasks", "implementation"}:
            raise ValueError(f"{source} field 'mode' must be `tasks` or `implementation`")
        updates["mode"] = mode

    if "priority" in data:
        priority = data["priority"]
        if priority not in VALID_TASK_PRIORITIES:
            allowed = ", ".join(sorted(VALID_TASK_PRIORITIES))
            raise ValueError(f"{source} field 'priority' must be one of: {allowed}")
        updates["priority"] = priority

    if "model" in data:
        model = data["model"]
        if model is not None and not isinstance(model, str):
            raise ValueError(f"{source} field 'model' must be a string or null")
        updates["model"] = model

    if "retry_limit" in data:
        retry_limit = data["retry_limit"]
        if retry_limit is not None and (not isinstance(retry_limit, int) or retry_limit < 0):
            raise ValueError(f"{source} field 'retry_limit' must be an integer >= 0 or null")
        updates["retry_limit"] = retry_limit

    if "auto_commit" in data:
        auto_commit = data["auto_commit"]
        if not isinstance(auto_commit, bool):
            raise ValueError(f"{source} field 'auto_commit' must be true or false")
        updates["auto_commit"] = auto_commit

    if "outcome" in data:
        outcome = data["outcome"]
        if outcome is not None and outcome not in _VALID_CLOSE_OUTCOMES:
            allowed = ", ".join(sorted(_VALID_CLOSE_OUTCOMES))
            raise ValueError(f"{source} field 'outcome' must be one of: {allowed}, or null")
        if outcome is not None:
            updates["outcome"] = outcome

    if "outcome_reason" in data:
        outcome_reason = data["outcome_reason"]
        if outcome_reason is not None and not isinstance(outcome_reason, str):
            raise ValueError(f"{source} field 'outcome_reason' must be a string or null")
        if outcome_reason is not None:
            updates["outcome_reason"] = outcome_reason

    if "action" in data:
        action = data["action"]
        if action is not None and action not in _VALID_ACTIONS:
            allowed = ", ".join(sorted(_VALID_ACTIONS))
            raise ValueError(f"{source} field 'action' must be one of: {allowed}, or null")
        if action is not None:
            updates["action"] = action

    if not updates:
        raise ValueError(f"{source} does not contain any supported task fields to update")

    return updates


def _editable_task_update_payload(task):
    return {
        "title": task.title,
        "goal": task.goal,
        "acceptance_criteria": list(task.acceptance_criteria),
        "constraints": list(task.constraints),
        "plan": list(task.plan),
        "pm_complexity": task.pm_complexity,
        "planned_effort": task.planned_effort,
        "depends_on": list(task.depends_on),
        "human_checkpoints": list(task.human_checkpoints),
        "task_type": task.task_type,
        "mode": task.mode,
        "priority": task.priority,
        "model": task.model,
        "retry_limit": task.retry_policy.max_retries,
        "auto_commit": task.git.auto_commit,
    }


def _render_task_update_template(task):
    lines = [
        "# Edit the fields you want Litehive to persist for this task.",
        "# This document uses YAML and writes back into the existing task record fields.",
        "# Use [] to clear list fields, null to clear optional scalar fields, and keep strings multi-line with | when needed.",
        "",
    ]
    payload = yaml.safe_dump(
        _editable_task_update_payload(task),
        sort_keys=False,
        allow_unicode=False,
    ).rstrip()
    lines.append(payload)
    lines.append("")
    return "\n".join(lines)


def load_rich_task_update_file(path):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not valid YAML: {exc}") from exc
    return parse_rich_task_update_document(data, source=str(path))


def _resolve_editor_command():
    raw = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if raw is None or not raw.strip():
        raise ValueError("No editor configured. Set $VISUAL or $EDITOR, or use --from-file.")
    return shlex.split(raw)


def collect_editor_task_updates(workspace, task_id):
    task = require_task(workspace, task_id)
    editor_cmd = _resolve_editor_command()
    fd, raw_path = tempfile.mkstemp(prefix=f"litehive-{task.id.lower()}-", suffix=".yaml")
    edit_path = __import__("pathlib").Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_render_task_update_template(task))
        proc = subprocess.run([*editor_cmd, str(edit_path)], check=False)
        if proc.returncode != 0:
            raise ValueError(f"Editor exited with status {proc.returncode}")
        return load_rich_task_update_file(edit_path)
    finally:
        edit_path.unlink(missing_ok=True)


def merge_task_updates(
    base_updates,
    overlay_updates,
    *,
    overlay_source,
):
    conflicts = sorted(set(base_updates) & set(overlay_updates))
    if conflicts:
        raise ValueError(
            f"{overlay_source} overlaps with another update source for field(s): {', '.join(conflicts)}"
        )
    merged = dict(base_updates)
    merged.update(overlay_updates)
    return merged


def parse_human_checkpoints(
    raw_values,
    *,
    allow_clear=False,
):
    if not raw_values:
        return ...

    stripped = [item.strip() for item in raw_values if item.strip()]
    if allow_clear and len(stripped) == 1 and stripped[0].lower() == "none":
        return []
    if any(item.lower() == "none" for item in stripped):
        raise ValueError("'none' can only be used by itself")
    normalized = normalize_human_checkpoints(stripped)
    if not normalized:
        raise ValueError("Human checkpoints must not be empty")
    return normalized
