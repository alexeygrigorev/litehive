from pathlib import Path
from typing import Annotated

import typer
from heru import ENGINE_CHOICES, get_engine
from heru.quota import (
    UsageStatus,
    check_claude_quota,
    check_codex_quota,
    check_copilot_quota,
    check_zai_quota,
)

from litehive.cli.common import WorkspaceOption, choice
from litehive.config.engine_models import clear_persisted_engine_freeze, parse_engine_freeze_until, persist_engine_freeze_iso
from litehive.config.loading import load_config
from litehive.domain.engine import EngineUsageRecord
from litehive.observability.engine_monitoring import load_engine_monitoring


def engine_command(
    workspace: WorkspaceOption = Path.cwd(),
    action: Annotated[str, typer.Argument(click_type=choice(["freeze", "status", "unfreeze"]), help="Subcommand")] = ...,
    name: Annotated[str | None, typer.Argument(help="Engine name for freeze/unfreeze")] = None,
    until: Annotated[str | None, typer.Option(help="Freeze until this ISO date (YYYY-MM-DD)")] = None,
    reason: Annotated[str | None, typer.Option(help="Operator note")] = None,
) -> int:
    load_config(workspace)
    if action == "status":
        if name:
            print("engine status: does not take positional arguments")
            return 1
        for line in _render_engine_status_lines(workspace.resolve()):
            print(line)
        return 0
    if name not in ENGINE_CHOICES:
        print(f"engine {action}: unknown engine '{name}'")
        return 1
    if action == "freeze":
        freeze_iso = parse_engine_freeze_until(until)
        if freeze_iso is None:
            print("engine freeze: --until must be ISO date YYYY-MM-DD")
            return 1
        persist_engine_freeze_iso(workspace, engine_name=name, freeze_iso=freeze_iso)
        print(f"engine_frozen: {name} until {freeze_iso}" + (f" reason={reason}" if reason else ""))
        return 0
    if not clear_persisted_engine_freeze(workspace, engine_name=name):
        print(f"engine unfreeze: {name} is not frozen")
        return 1
    print(f"engine_unfrozen: {name}")
    return 0


def _render_engine_status_lines(root: Path) -> list[str]:
    config = load_config(root)
    monitoring = load_engine_monitoring(root)
    quota_statuses = _collect_quota_statuses()
    lines = [
        f"default_engine: {config.default_engine}",
        f"engine_freeze: {', '.join(f'{k}={v}' for k, v in sorted(config.engine_freeze.items())) or '-'}",
    ]
    for engine_name in ENGINE_CHOICES:
        caps = get_engine(engine_name).capabilities
        lines.extend(
            [
                "",
                (
                    f"engine: {engine_name} "
                    f"available={'yes' if caps.available else 'no'} "
                    f"model_override={'yes' if caps.supports_model_override else 'no'} "
                    f"strips_env={'yes' if caps.strips_environment else 'no'} "
                    f"frozen_until={config.engine_freeze.get(engine_name, '-')}"
                ),
                _render_monitoring_line(monitoring.engines.get(engine_name)),
                _render_quota_line(engine_name, quota_statuses[engine_name]),
            ]
        )
    return lines


def _collect_quota_statuses() -> dict[str, object]:
    zai_status = _safe_quota_check(check_zai_quota)
    return {
        "claude": _safe_quota_check(check_claude_quota),
        "codex": _safe_quota_check(check_codex_quota),
        "copilot": _safe_quota_check(check_copilot_quota),
        "gemini": "unsupported",
        "goz": zai_status,
        "opencode": zai_status,
    }


def _safe_quota_check(checker) -> object:
    try:
        return checker()
    except Exception as exc:  # pragma: no cover - defensive fallback
        return UsageStatus(error=_quota_error_label(exc))


def _quota_error_label(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def _render_monitoring_line(record: EngineUsageRecord | None) -> str:
    if record is None:
        return "monitoring: no data"
    parts = [
        "monitoring:",
        f"source={record.source}",
        f"invocations={record.invocation_count}",
        f"success={record.success_count}",
        f"failure={record.failure_count}",
        f"limits={record.limit_event_count}",
    ]
    if record.provider:
        parts.append(f"provider={record.provider}")
    if record.last_limit_kind:
        parts.append(f"last_limit_kind={record.last_limit_kind}")
    if record.last_limit_reason:
        parts.append(f"last_limit_reason={record.last_limit_reason}")
    if record.usage is not None:
        usage_parts: list[str] = []
        if record.usage.used is not None:
            usage_parts.append(f"used={record.usage.used}")
        if record.usage.limit is not None:
            usage_parts.append(f"limit={record.usage.limit}")
        if record.usage.remaining is not None:
            usage_parts.append(f"remaining={record.usage.remaining}")
        if record.usage.unit:
            usage_parts.append(f"unit={record.usage.unit}")
        if record.usage.reset_at:
            usage_parts.append(f"reset_at={record.usage.reset_at}")
        if usage_parts:
            parts.append("usage=" + ",".join(usage_parts))
    if record.observed_at:
        parts.append(f"observed_at={record.observed_at}")
    return " ".join(parts)


def _render_quota_line(engine_name: str, status: object) -> str:
    quota_error = _quota_status_error(status)
    if quota_error is not None:
        return quota_error
    if engine_name == "claude":
        return (
            "quota: "
            f"5h utilization={status.five_hour.used_percent:.1f}% reset={status.five_hour.reset_at or '-'} | "
            f"7d utilization={status.seven_day.used_percent:.1f}% reset={status.seven_day.reset_at or '-'}"
        )
    if engine_name == "codex":
        return (
            "quota: "
            f"5h used={status.primary_window.used_percent:.1f}% reset={status.primary_window.reset_at or '-'} | "
            f"weekly used={status.secondary_window.used_percent:.1f}% reset={status.secondary_window.reset_at or '-'}"
        )
    if engine_name == "copilot":
        remaining = status.premium_remaining if status.premium_remaining is not None else "-"
        entitlement = status.premium_entitlement if status.premium_entitlement is not None else "-"
        return (
            "quota: "
            f"premium remaining={remaining}/{entitlement} "
            f"used={status.used_percent:.1f}% "
            f"reset={status.quota_reset_date or '-'}"
        )
    return (
        "quota: "
        f"api calls used={status.api_calls.used_percent:.1f}% "
        f"remaining={_remaining_label(status.api_calls.remaining, status.api_calls.limit)} "
        f"window={_window_hours_label(status.api_calls.window_hours)} | "
        f"tokens used={status.tokens.used_percent:.1f}% "
        f"remaining={_remaining_label(status.tokens.remaining, status.tokens.limit)} "
        f"window={_window_hours_label(status.tokens.window_hours)}"
    )


def _quota_status_error(status: object) -> str | None:
    if status == "unsupported":
        return "quota: unsupported"
    error = getattr(status, "error", None)
    if error is not None:
        return f"quota: unavailable ({error})"
    return None


def _remaining_label(remaining: int | None, limit: int | None) -> str:
    remaining_label = remaining if remaining is not None else "-"
    limit_label = limit if limit is not None else "-"
    return f"{remaining_label}/{limit_label}"


def _window_hours_label(window_hours: int | None) -> str:
    if window_hours is None:
        return "-"
    return f"{window_hours}h"
