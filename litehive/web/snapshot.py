from dataclasses import asdict
from pathlib import Path
from typing import Any

from litehive.config import load_config
from litehive.agents.quota import (
    check_claude_quota,
    check_codex_quota,
    check_copilot_quota,
    check_zai_quota,
)
from litehive.models import TaskRecord
from litehive.observability import load_engine_monitoring
from litehive.pipeline import active_engine_freezes
from litehive.events import read_events
from litehive.tasks import (
    VALID_TASK_ENGINES,
    VALID_TASK_PRIORITIES,
    VALID_TASK_TYPES,
    list_tasks_state_first,
    load_state,
    load_task_thread,
    runner_status_readonly,
    task_dir,
)

from litehive.web.common import (
    _MAX_ARTIFACT_BYTES,
    _RUN_ALL_LOG_LIMIT,
    _RUN_ALL_PREVIEW_BYTES,
    _SESSION_LIMIT,
    _WEB_REVIEWABLE_STAGES,
    SessionArtifact,
    _engine_attempt_order,
    _load_yaml_file,
    _mtime,
    _read_iso_now,
    _read_text_file,
    _relative_to_root,
    _resolve_artifact_path,
    _serialize_engine_record,
)


def build_workspace_snapshot(root: Path) -> dict[str, Any]:
    root = root.resolve()
    state = load_state(root)
    runner = runner_status_readonly(root)
    tasks = list_tasks_state_first(root, state=state, include_runtime=True)
    tasks_payload = [_serialize_task(root, task, state.active_task_id) for task in tasks]
    active_task = next((task for task in tasks if task.id == state.active_task_id), None)
    return {
        "workspace": str(root),
        "generated_at": _read_iso_now(),
        "runner": runner.model_dump(mode="python"),
        "daemon": build_daemon_status_payload(root),
        "state": state.model_dump(mode="python"),
        "editable_fields": {
            "priority_options": sorted(VALID_TASK_PRIORITIES),
            "engine_options": sorted(VALID_TASK_ENGINES),
        },
        "queue": list(state.queue),
        "active_task_id": state.active_task_id,
        "active_task": None
        if active_task is None
        else _serialize_task(root, active_task, state.active_task_id),
        "tasks": tasks_payload,
        "run_all_logs": list_recent_run_all_logs(root),
    }


def build_daemon_status_payload(root: Path) -> dict[str, Any]:
    from litehive import web as _web_pkg

    root = root.resolve()
    entry = _web_pkg.get_workspace_daemon(root)
    daemon_status = "running" if entry is not None else "stopped"
    started_at = entry.get("started_at") if isinstance(entry, dict) else None
    uptime_seconds = _uptime_seconds(str(started_at) if started_at is not None else None)
    return {
        "workspace": str(root),
        "daemon_status": daemon_status,
        "pid": entry.get("pid") if isinstance(entry, dict) else None,
        "started_at": started_at if isinstance(started_at, str) else None,
        "uptime_seconds": uptime_seconds,
        "uptime": _format_duration(uptime_seconds),
        "log_dir": entry.get("log_dir") if isinstance(entry, dict) else None,
        "recent_logs": _web_pkg.list_recent_run_all_logs(root),
    }


def read_engine_dashboard(root: Path) -> dict[str, Any]:
    root = root.resolve()
    config = load_config(root)
    monitoring = load_engine_monitoring(root)
    active_freezes = active_engine_freezes(config)
    default_fallback_order = _engine_attempt_order(config.default_engine, config.engine_preference)
    return {
        "config": {
            "default_engine": config.default_engine,
            "recovery_engine": config.recovery_engine,
            "claude_enabled": config.claude_enabled,
            "engine_preference": list(config.engine_preference),
            "engine_freeze": dict(config.engine_freeze),
            "active_engine_freezes": {
                engine_name: freeze_dt.replace(microsecond=0).isoformat()
                for engine_name, freeze_dt in sorted(active_freezes.items())
            },
            "models": {
                "codex": config.codex_model,
                "opencode": config.opencode_model,
                "gemini": config.gemini_model,
                "copilot": config.copilot_model,
                "claude": config.claude_model if config.claude_enabled else None,
                "goz": config.goz_model,
            },
            "engine_usage_caps": dict(config.engine_usage_caps),
            "engine_budget_caps": dict(config.engine_budget_caps),
            "engine_costs": dict(config.engine_costs),
        },
        "routing": {
            "precedence": [
                {
                    "order": 1,
                    "rule": "task_override",
                    "description": "Task engine override wins when task.yaml sets engine.",
                },
                {
                    "order": 2,
                    "rule": "workspace_default",
                    "description": "Otherwise Litehive uses the workspace default engine.",
                },
                {
                    "order": 3,
                    "rule": "fallback_preference",
                    "description": "Retries and execution-limit fallbacks follow engine_preference after the selected primary engine.",
                },
                {
                    "order": 4,
                    "rule": "freeze_filter",
                    "description": "Engines with an active freeze are skipped from the runnable attempt order.",
                },
            ],
            "default_fallback_order": default_fallback_order,
            "task_types": [
                {
                    "task_type": task_type,
                    "configured_engine": None,
                    "effective_engine": config.default_engine,
                    "fallback_order": default_fallback_order,
                    "source": "workspace default",
                }
                for task_type in sorted(VALID_TASK_TYPES)
            ],
        },
        "monitoring": {
            "engines": {
                engine_name: _serialize_engine_record(record.model_dump(mode="python"))
                for engine_name, record in sorted(monitoring.engines.items())
            }
        },
        "quota": {
            "engines": _read_engine_quota_snapshot(),
        },
        "editable_fields": {
            "engine_options": sorted(VALID_TASK_ENGINES),
        },
    }


def _read_engine_quota_snapshot() -> dict[str, dict[str, Any]]:
    zai_status = check_zai_quota()
    snapshots = {
        "claude": _serialize_claude_quota(check_claude_quota()),
        "codex": _serialize_codex_quota(check_codex_quota()),
        "copilot": _serialize_copilot_quota(check_copilot_quota()),
        "goz": _serialize_zai_quota(zai_status),
        "opencode": _serialize_zai_quota(zai_status),
    }
    return {engine_name: snapshots[engine_name] for engine_name in sorted(snapshots)}


def _serialize_codex_quota(status: Any) -> dict[str, Any]:
    error = getattr(status, "error", None)
    primary_window = getattr(status, "primary_window", None)
    secondary_window = getattr(status, "secondary_window", None)
    windows = [
        _build_percent_window(
            "5h",
            getattr(primary_window, "used_percent", None),
            getattr(primary_window, "reset_at", None),
        ),
        _build_percent_window(
            "weekly",
            getattr(secondary_window, "used_percent", None),
            getattr(secondary_window, "reset_at", None),
        ),
    ]
    return {
        "engine": "codex",
        "provider": "openai",
        "status": "unavailable" if error else "ok",
        "error": error,
        "summary": "unavailable" if error else _max_window_summary(windows),
        "windows": windows,
    }


def _serialize_claude_quota(status: Any) -> dict[str, Any]:
    error = getattr(status, "error", None)
    five_hour = getattr(status, "five_hour", None)
    seven_day = getattr(status, "seven_day", None)
    windows = [
        _build_percent_window(
            "5h",
            getattr(five_hour, "used_percent", None),
            getattr(five_hour, "reset_at", None),
        ),
        _build_percent_window(
            "7d",
            getattr(seven_day, "used_percent", None),
            getattr(seven_day, "reset_at", None),
        ),
    ]
    return {
        "engine": "claude",
        "provider": "anthropic",
        "status": "unavailable" if error else "ok",
        "error": error,
        "summary": "unavailable" if error else _max_window_summary(windows),
        "windows": windows,
    }


def _serialize_copilot_quota(status: Any) -> dict[str, Any]:
    error = getattr(status, "error", None)
    remaining = getattr(status, "premium_remaining", None)
    entitlement = getattr(status, "premium_entitlement", None)
    used_percent = getattr(status, "used_percent", None)
    reset_at = getattr(status, "quota_reset_date", None)
    return {
        "engine": "copilot",
        "provider": "github",
        "status": "unavailable" if error else "ok",
        "error": error,
        "summary": (
            "unavailable"
            if error
            else (
                f"{_format_percent(used_percent)} used, "
                f"{_format_ratio(remaining, entitlement)} premium requests remaining"
            )
        ),
        "windows": [
            {
                "label": "premium",
                "used_percent": _round_float(used_percent),
                "remaining": remaining,
                "limit": entitlement,
                "remaining_display": _format_ratio(remaining, entitlement),
                "reset_at": reset_at,
            }
        ],
    }


def _serialize_zai_quota(status: Any) -> dict[str, Any]:
    error = getattr(status, "error", None)
    api_calls = getattr(status, "api_calls", None)
    tokens = getattr(status, "tokens", None)
    windows = [
        _build_usage_window(
            "api calls",
            getattr(api_calls, "used_percent", None),
            getattr(api_calls, "remaining", None),
            getattr(api_calls, "limit", None),
            f"{getattr(api_calls, 'window_hours', 0)}h" if getattr(api_calls, "window_hours", 0) else None,
        ),
        _build_usage_window(
            "tokens",
            getattr(tokens, "used_percent", None),
            getattr(tokens, "remaining", None),
            getattr(tokens, "limit", None),
            f"{getattr(tokens, 'window_hours', 0)}h" if getattr(tokens, "window_hours", 0) else None,
        ),
    ]
    summary = _max_window_summary(windows)
    return {
        "provider": "z.ai",
        "status": "unavailable" if error else "ok",
        "error": error,
        "summary": "unavailable" if error else summary,
        "windows": windows,
    }


def _build_percent_window(label: str, used_percent: Any, reset_at: str | None) -> dict[str, Any]:
    rounded_used = _round_float(used_percent)
    remaining_percent = None if rounded_used is None else max(0.0, round(100.0 - rounded_used, 1))
    return {
        "label": label,
        "used_percent": rounded_used,
        "remaining_percent": remaining_percent,
        "reset_at": reset_at,
    }


def _build_usage_window(
    label: str,
    used_percent: Any,
    remaining: Any,
    limit: Any,
    window: str | None,
) -> dict[str, Any]:
    rounded_used = _round_float(used_percent)
    return {
        "label": label,
        "window": window,
        "used_percent": rounded_used,
        "remaining": remaining,
        "limit": limit,
        "remaining_display": _format_ratio(remaining, limit),
    }


def _format_ratio(remaining: Any, limit: Any) -> str:
    if remaining is None and limit is None:
        return "-"
    return f"{remaining if remaining is not None else '-'}/{limit if limit is not None else '-'}"


def _round_float(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


def _format_percent(value: Any) -> str:
    if value is None:
        return "-"
    rounded = round(float(value), 1)
    if rounded.is_integer():
        return f"{int(rounded)}%"
    return f"{rounded:.1f}%"


def _max_window_summary(windows: list[dict[str, Any]]) -> str:
    available = [window for window in windows if window.get("used_percent") is not None]
    if not available:
        return "-"
    highest = max(available, key=lambda item: float(item["used_percent"]))
    return f"{highest['label']} {_format_percent(highest['used_percent'])} used"


def list_recent_run_all_logs(
    root: Path, *, limit: int = _RUN_ALL_LOG_LIMIT
) -> list[dict[str, Any]]:
    logs_root = root / ".litehive" / "logs" / "run-all"
    if not logs_root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for directory in sorted((path for path in logs_root.iterdir() if path.is_dir()), reverse=True)[
        :limit
    ]:
        files: list[dict[str, Any]] = []
        for file_path in sorted((path for path in directory.iterdir() if path.is_file())):
            preview = _read_text_file(file_path, max_bytes=_RUN_ALL_PREVIEW_BYTES)
            files.append(
                {
                    "name": file_path.name,
                    "path": _relative_to_root(root, file_path),
                    "size": file_path.stat().st_size,
                    "modified_at": _mtime(file_path),
                    "preview": preview["content"],
                    "truncated": preview["truncated"],
                }
            )
        entries.append(
            {
                "name": directory.name,
                "path": _relative_to_root(root, directory),
                "modified_at": _mtime(directory),
                "files": files,
            }
        )
    return entries


def read_session_view(root: Path, task_id: str, subagent_id: str) -> dict[str, Any]:
    root = root.resolve()
    task = next(
        (item for item in list_tasks_state_first(root, include_runtime=True) if item.id == task_id),
        None,
    )
    if task is None:
        raise FileNotFoundError(f"Task {task_id} not found")
    ref = next((item for item in task.subagents if item.id == subagent_id), None)
    if ref is None:
        raise FileNotFoundError(f"Subagent {subagent_id} not found for task {task_id}")

    base = task_dir(root, task) / ref.path
    session = _load_yaml_file(base / "session.yaml")
    active_subagent = task.runtime.active_subagent
    is_active = bool(active_subagent and active_subagent.id == subagent_id)
    status = str(session.get("status") or ("running" if is_active else ref.status))
    artifacts = [
        _read_session_artifact(root, base, "transcript", active=is_active),
        _read_session_artifact(root, base, "stdout", active=is_active),
        _read_session_artifact(root, base, "stderr", active=is_active),
    ]
    return {
        "task_id": task.id,
        "task_title": task.title,
        "subagent_id": ref.id,
        "role": ref.role,
        "engine": ref.engine,
        "status": status,
        "is_active": is_active,
        "session_path": _relative_to_root(root, base / "session.yaml"),
        "session": session,
        "artifacts": [asdict(artifact) for artifact in artifacts],
    }


def _serialize_task(root: Path, task: TaskRecord, active_task_id: str | None) -> dict[str, Any]:
    base = task_dir(root, task)
    session_entries: list[dict[str, Any]] = []
    sorted_refs = sorted(task.subagents, key=lambda item: item.id, reverse=True)[:_SESSION_LIMIT]
    active_subagent = task.runtime.active_subagent
    last_subagent = task.runtime.last_subagent
    for ref in sorted_refs:
        session_base = base / ref.path
        session = _load_yaml_file(session_base / "session.yaml")
        is_active = bool(active_subagent and active_subagent.id == ref.id)
        artifact_sources = {
            "transcript": _artifact_path(root, session_base, "transcript", active=is_active),
            "stdout": _artifact_path(root, session_base, "stdout", active=is_active),
            "stderr": _artifact_path(root, session_base, "stderr", active=is_active),
        }
        session_entries.append(
            {
                "id": ref.id,
                "role": ref.role,
                "engine": ref.engine,
                "status": str(session.get("status") or ref.status),
                "is_active": is_active,
                "path": ref.path,
                "updated_at": session.get("updated_at"),
                "pid": session.get("pid"),
                "exit_code": session.get("exit_code"),
                "tail_targets": artifact_sources,
            }
        )

    reports = _load_stage_reports(root, base)
    recovery_reports = _load_recovery_reports(root, base)
    thread = [comment.model_dump(mode="python") for comment in load_task_thread(root, task)]
    events = _load_task_events(root, task)
    record = task.model_dump(mode="python", exclude={"runtime"})

    return {
        "id": task.id,
        "slug": task.slug,
        "title": task.title,
        "status": task.status,
        "pipeline_status": task.pipeline_status,
        "priority": task.priority,
        "goal": task.goal,
        "acceptance_criteria": list(task.acceptance_criteria),
        "plan": list(task.plan),
        "constraints": list(task.constraints),
        "can_submit_verdict": task.id == active_task_id
        and task.pipeline_status in _WEB_REVIEWABLE_STAGES,
        "is_active_task": task.id == active_task_id,
        "runtime": task.runtime.model_dump(mode="python"),
        "current_stage": task.runtime.current_stage.model_dump(mode="python"),
        "last_stage": task.runtime.last_stage.model_dump(mode="python"),
        "active_subagent": None
        if active_subagent is None
        else active_subagent.model_dump(mode="python"),
        "last_subagent": None if last_subagent is None else last_subagent.model_dump(mode="python"),
        "task_path": _relative_to_root(root, base),
        "task_file": _relative_to_root(root, base / "task.yaml"),
        "runtime_file": _relative_to_root(root, base / "runtime.yaml"),
        "thread_file": _relative_to_root(root, base / "thread.yaml"),
        "events_file": _relative_to_root(root, base / "events.jsonl"),
        "reports_dir": _relative_to_root(root, base / "reports"),
        "recovery_dir": _relative_to_root(root, base / "recovery"),
        "record": record,
        "reports": reports,
        "events": events,
        "thread": thread,
        "recovery_reports": recovery_reports,
        "subagents": session_entries,
    }


def _load_stage_reports(root: Path, base: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    reports_dir = base / "reports"
    if not reports_dir.exists():
        return reports
    for report_path in sorted(reports_dir.glob("*.yaml")):
        payload = _load_yaml_file(report_path)
        reports.append(
            {
                "name": report_path.name,
                "path": _relative_to_root(root, report_path),
                "step": payload.get("step"),
                "verdict": payload.get("verdict"),
                "summary": payload.get("summary"),
                "created_at": payload.get("created_at"),
                "report": payload,
            }
        )
    return reports


def _load_recovery_reports(root: Path, base: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    recovery_dir = base / "recovery"
    if not recovery_dir.exists():
        return reports
    for report_path in sorted(recovery_dir.glob("*.yaml")):
        payload = _load_yaml_file(report_path)
        reports.append(
            {
                "name": report_path.name,
                "path": _relative_to_root(root, report_path),
                "summary": payload.get("summary"),
                "trigger": payload.get("trigger"),
                "stage": payload.get("stage"),
                "runnable_state": payload.get("runnable_state"),
                "created_at": payload.get("created_at"),
                "report": payload,
            }
        )
    return reports


def _load_task_events(root: Path, task: TaskRecord) -> list[dict[str, Any]]:
    events = read_events(root, task)
    events.sort(
        key=lambda item: (
            str(item.get("ts") or ""),
            int(item.get("sequence", 0)) if isinstance(item.get("sequence", 0), int) else 0,
        )
    )
    return events


def _read_session_artifact(root: Path, base: Path, kind: str, *, active: bool) -> SessionArtifact:
    if kind == "transcript":
        path = _resolve_artifact_path(base, "transcript.md")
        display_path = path if path is not None else base / "transcript.md"
        payload = _read_text_file(path, max_bytes=_MAX_ARTIFACT_BYTES)
        return SessionArtifact(
            kind=kind,
            label="Transcript",
            path=_relative_to_root(root, display_path),
            source="rewrite",
            content=payload["content"],
            size=payload["size"],
            truncated=payload["truncated"],
            available=payload["available"],
        )

    preferred = base / f"{kind}.log" if active else base / f"{kind}.txt"
    fallback = base / f"{kind}.txt"
    gzip_fallback = base / f"{kind}.txt.gz"
    source_path = preferred
    source_type = "append-only log" if active else "final snapshot"
    if not source_path.exists():
        if fallback.exists():
            source_path = fallback
            source_type = "final snapshot"
        elif gzip_fallback.exists():
            source_path = gzip_fallback
            source_type = "compressed final snapshot"
        else:
            source_path = preferred
    payload = _read_text_file(source_path, max_bytes=_MAX_ARTIFACT_BYTES)
    return SessionArtifact(
        kind=kind,
        label=kind.upper(),
        path=_relative_to_root(root, source_path),
        source=source_type,
        content=payload["content"],
        size=payload["size"],
        truncated=payload["truncated"],
        available=payload["available"],
    )


def _serialize_task_with_queue_metadata(root: Path, task: TaskRecord) -> dict[str, Any]:
    state = load_state(root)
    payload = _serialize_task(root, task, state.active_task_id)
    payload["queue_position"] = (
        None if task.id not in state.queue else state.queue.index(task.id) + 1
    )
    return payload


def _artifact_path(root: Path, base: Path, kind: str, *, active: bool) -> str:
    if kind == "transcript":
        path = _resolve_artifact_path(base, "transcript.md")
        return _relative_to_root(root, path if path is not None else base / "transcript.md")
    candidates = (
        [base / f"{kind}.log", base / f"{kind}.txt", base / f"{kind}.txt.gz"]
        if active
        else [base / f"{kind}.txt", base / f"{kind}.txt.gz", base / f"{kind}.log"]
    )
    for candidate in candidates:
        if candidate.exists():
            return _relative_to_root(root, candidate)
    return _relative_to_root(root, candidates[0])


def _uptime_seconds(started_at: str | None) -> int | None:
    from datetime import UTC, datetime

    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    elapsed = int((datetime.now(UTC) - started).total_seconds())
    return max(elapsed, 0)


def _format_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
