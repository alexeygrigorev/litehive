from pathlib import Path
from typing import Any

import yaml

from litehive.config import config_path, load_config
from litehive.models import StageReport, TaskRecord, TaskThreadComment
from litehive.runner.states import _ROUTES
from litehive.tasks import (
    _apply_stage_finished,
    _apply_task_outcome,
    _persist_task_and_state_without_runner_guard,
    _workspace_lock,
    VALID_TASK_ENGINES,
    abandon_task,
    append_thread_comment,
    close_task,
    create_task,
    load_state,
    requeue_task,
    require_task,
    stop_current_task,
    switch_task_engine,
    task_dir,
    update_task,
)

from litehive.web.common import (
    _WEB_REVIEWABLE_STAGES,
    _WEB_VERDICT_OPTIONS,
    _coerce_text_list,
    _field_or_missing,
    _list_field_or_missing,
    _list_or_missing,
    _load_yaml_file,
    _optional_bool,
    _optional_string,
    _optional_string_list,
    _optional_update_priority,
    _require_string,
)
from litehive.web.snapshot import (
    _serialize_task,
    _serialize_task_with_queue_metadata,
    read_engine_dashboard,
)


def update_task_detail(root: Path, task_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    task = _require_task_for_web(root, task_id)
    payload: dict[str, Any] = {}
    for field in ("goal", "priority", "engine"):
        if field in updates:
            value = updates[field]
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be a string or null")
            payload[field] = value
    for field in ("acceptance_criteria", "constraints", "plan"):
        if field in updates:
            payload[field] = _coerce_text_list(field, updates[field])
    if not payload:
        raise ValueError("No supported fields to update")
    updated = update_task(
        root,
        task.id,
        journal_message="Task metadata updated via web dashboard.",
        **payload,
    )
    return {"task": _serialize_task(root, updated, load_state(root).active_task_id)}


def update_default_engine(root: Path, engine_name: str) -> dict[str, Any]:
    root = root.resolve()
    if engine_name not in VALID_TASK_ENGINES:
        raise ValueError(f"Unsupported engine '{engine_name}'")
    path = config_path(root)
    raw_data = _load_yaml_file(path)
    config = load_config(root)
    previous_engine = config.default_engine
    raw_data["default_engine"] = engine_name
    path.write_text(yaml.safe_dump(raw_data, sort_keys=False), encoding="utf-8")
    return {
        "default_engine": engine_name,
        "previous_default_engine": previous_engine,
        "engines": read_engine_dashboard(root),
    }


def switch_task_engine_via_web(
    root: Path,
    *,
    task_id: str,
    engine: str,
    reason: str,
) -> dict[str, Any]:
    root = root.resolve()
    try:
        summary = switch_task_engine(root, task_id, engine=engine, reason=reason)
    except ValueError as exc:
        if str(exc).startswith("Task ") and str(exc).endswith("not found"):
            raise FileNotFoundError(str(exc)) from exc
        raise
    task = require_task(root, task_id)
    return {
        "task": _serialize_task(root, task, load_state(root).active_task_id),
        "switch": {
            "previous_engine": summary.previous_engine,
            "new_engine": summary.new_engine,
            "was_active": summary.was_active,
            "runner_pid": summary.runner_pid,
            "signal_sent": summary.signal_sent,
            "prior_work_paths": list(summary.prior_work_paths),
        },
    }


def submit_stage_verdict_via_web(
    root: Path,
    *,
    task_id: str | None,
    role: str,
    step: str,
    verdict: str,
    message: str,
) -> dict[str, Any]:
    root = root.resolve()
    if verdict not in _WEB_VERDICT_OPTIONS:
        allowed = ", ".join(_WEB_VERDICT_OPTIONS)
        raise ValueError(f"Unsupported verdict '{verdict}'. Expected one of: {allowed}")

    with _workspace_lock(root):
        state = load_state(root)
        active_task_id = state.active_task_id
        if not active_task_id:
            raise ValueError("No active task available for report submission")
        if task_id is not None and task_id != active_task_id:
            raise ValueError(f"Task {task_id} is not the active task")

        task = _require_task_for_web(root, active_task_id)
        if task.pipeline_status not in _WEB_REVIEWABLE_STAGES:
            raise ValueError(
                "Report submission is only available for active tasks in testing or accepting"
            )
        if step != task.pipeline_status:
            raise ValueError(
                f"step must match the active task pipeline status '{task.pipeline_status}'"
            )

        cleaned_role = role.strip()
        cleaned_message = message.strip()
        comment = TaskThreadComment(
            role=cleaned_role,
            step=step,
            verdict=verdict,  # type: ignore[arg-type]
            message=cleaned_message,
        )
        append_thread_comment(root, task, comment)

        if verdict == "comment":
            return {
                "task": _serialize_task(root, task, state.active_task_id),
                "submitted": {
                    "task_id": task.id,
                    "step": step,
                    "verdict": verdict,
                    "role": cleaned_role,
                },
            }

        report = StageReport(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            verdict=verdict,  # type: ignore[arg-type]
            summary=cleaned_message,
            feedback=cleaned_message,
        )
        _write_stage_report(root, task, report)
        _apply_stage_finished(task, report)

        target = _ROUTES.get((step, verdict))
        if target == "accepting":
            task.pipeline_status = "accepting"
            task.status = "in_progress"
            task.runtime.execution_status = "running"
        elif target in {"implementing", "commit_to_git"}:
            task.pipeline_status = target
            task.status = "queued"
            task.runtime.execution_status = "queued"
            state.active_task_id = None
            state.queue = [item for item in state.queue if item != task.id]
            state.queue.insert(0, task.id)
        elif target == "done":
            task.pipeline_status = "done"
            task.status = "done"
            task.runtime.execution_status = "done"
            state.active_task_id = None
            state.queue = [item for item in state.queue if item != task.id]
        else:
            task.status = "flagged"
            task.runtime.execution_status = "flagged"
            from litehive.workspace.runtime_tracking import _apply_flag_count_auto_defer
            _apply_flag_count_auto_defer(task)
            state.active_task_id = None
            state.queue = [item for item in state.queue if item != task.id]
            _apply_task_outcome(
                task,
                kind="blocked" if verdict == "blocked" else "flagged",
                stage=step,
                reason_code="verdict_blocked" if verdict == "blocked" else "unsupported_verdict",
                reason=cleaned_message,
                retry_count=0,
                retry_limit=0,
                retry_source="global",
            )

        _persist_task_and_state_without_runner_guard(root, task=task, state=state)
        return {
            "task": _serialize_task(root, task, state.active_task_id),
            "submitted": {
                "task_id": task.id,
                "step": step,
                "verdict": verdict,
                "role": cleaned_role,
            },
        }


# ---------------------------------------------------------------------------
# Task mutation actions (create, update, close, requeue, abandon, stop)
# ---------------------------------------------------------------------------


def create_task_via_web(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    title = _require_string(payload, "title")
    task = create_task(
        root,
        title=title,
        goal=_optional_string(payload, "goal") or "",
        priority=_optional_string(payload, "priority"),
        engine=_optional_string(payload, "engine"),
        acceptance_criteria=_optional_string_list(payload, "acceptance_criteria"),
    )
    return {
        "ok": True,
        "task_id": task.id,
        "task": _serialize_task_with_queue_metadata(root, task),
    }


def update_task_via_web(root: Path, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    if not payload:
        raise ValueError("Update payload must include at least one field")
    plan = payload.get("plan", payload.get("plan_steps", ...))
    task = update_task(
        root,
        task_id,
        goal=_field_or_missing(payload, "goal"),
        priority=_optional_update_priority(payload),
        engine=_field_or_missing(payload, "engine"),
        acceptance_criteria=_list_field_or_missing(payload, "acceptance_criteria"),
        constraints=_list_field_or_missing(payload, "constraints"),
        plan=_list_or_missing(plan, "plan"),
        journal_message="Task updated via web dashboard.",
    )
    return {
        "ok": True,
        "task_id": task.id,
        "task": _serialize_task_with_queue_metadata(root, task),
    }


def close_task_via_web(root: Path, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    outcome = _require_string(payload, "outcome")
    reason = _optional_string(payload, "reason")
    task = close_task(root, task_id, outcome=outcome, reason=reason)
    return {
        "ok": True,
        "task_id": task.id,
        "task": _serialize_task_with_queue_metadata(root, task),
    }


def requeue_task_via_web(root: Path, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    front = _optional_bool(payload, "front", default=False)
    task = requeue_task(root, task_id, front=front)
    serialized = _serialize_task_with_queue_metadata(root, task)
    return {
        "ok": True,
        "task_id": task.id,
        "front": front,
        "queue_position": serialized["queue_position"],
        "task": serialized,
    }


def abandon_task_via_web(root: Path, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    if payload:
        raise ValueError("Abandon endpoint does not accept request fields")
    task = abandon_task(root, task_id)
    return {
        "ok": True,
        "task_id": task.id,
        "task": _serialize_task_with_queue_metadata(root, task),
    }


def stop_active_task_via_web(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    if payload:
        raise ValueError("Stop endpoint does not accept request fields")
    summary = stop_current_task(root)
    return {
        "ok": True,
        "task_id": summary.task.id,
        "task": _serialize_task_with_queue_metadata(root, summary.task),
        "runner_pid": summary.runner_pid,
        "signal_sent": summary.signal_sent,
    }


def _require_task_for_web(root: Path, task_id: str) -> TaskRecord:
    try:
        return require_task(root, task_id)
    except ValueError as exc:
        raise FileNotFoundError(str(exc)) from exc


def _write_stage_report(root: Path, task: TaskRecord, report: StageReport) -> Path:
    reports_dir = task_dir(root, task) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ordinal = len(list(reports_dir.glob(f"{report.step}-*.yaml"))) + 1
    path = reports_dir / f"{report.step}-{ordinal:03d}.yaml"
    path.write_text(
        yaml.safe_dump(report.model_dump(mode="python"), sort_keys=False), encoding="utf-8"
    )
    return path
