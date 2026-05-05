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
from litehive.config.engine_models import (
    active_engine_freezes,
    clear_persisted_engine_freeze,
    parse_engine_freeze_until,
    persist_engine_freeze_iso,
)
from litehive.config.loading import load_config
from litehive.config.model import normalize_engine_sequence
from litehive.config.runtime_settings import (
    load_runtime_setting_audit_entries,
    set_default_engine,
    set_engine_preference,
)


def engine_command(
    workspace: WorkspaceOption = Path.cwd(),
    action: Annotated[
        str,
        typer.Argument(
            click_type=choice(["audit", "default", "freeze", "preference", "status", "unfreeze"]),
            help="Subcommand",
        ),
    ] = ...,
    name: Annotated[str | None, typer.Argument(help="Engine name, setting key, or comma-separated engine list")] = None,
    until: Annotated[str | None, typer.Option(help="Freeze until this ISO date (YYYY-MM-DD)")] = None,
    reason: Annotated[str | None, typer.Option(help="Operator note")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum audit rows to show")] = 20,
) -> int:
    load_config(workspace)
    if action == "status":
        if name:
            print("engine status: does not take positional arguments")
            return 1
        for line in _render_engine_status_lines(workspace.resolve()):
            print(line)
        return 0
    if action == "audit":
        for line in _render_engine_audit_lines(workspace.resolve(), key=name, limit=limit):
            print(line)
        return 0
    if action == "default":
        if name not in ENGINE_CHOICES:
            print(f"engine default: unknown engine '{name}'")
            return 1
        change = set_default_engine(
            workspace,
            name,
            actor="operator",
            source="cli",
            context={"reason": reason} if reason else None,
        )
        print(f"default_engine: {change.old_value} -> {change.new_value}")
        print(f"updated: {'yes' if change.changed else 'no'}")
        return 0
    if action == "preference":
        try:
            preference = _parse_engine_preference(name)
        except ValueError as exc:
            print(f"engine preference: {exc}")
            return 1
        if preference is None:
            print("engine preference: provide a comma-separated engine list")
            return 1
        try:
            change = set_engine_preference(
                workspace,
                preference,
                actor="operator",
                source="cli",
                context={"reason": reason} if reason else None,
            )
        except ValueError as exc:
            print(f"engine preference: {exc}")
            return 1
        print(f"engine_preference: {_engine_list_label(change.old_value)} -> {_engine_list_label(change.new_value)}")
        print(f"updated: {'yes' if change.changed else 'no'}")
        return 0
    if name not in ENGINE_CHOICES:
        print(f"engine {action}: unknown engine '{name}'")
        return 1
    if action == "freeze":
        freeze_iso = parse_engine_freeze_until(until)
        if freeze_iso is None:
            print("engine freeze: --until must be ISO date YYYY-MM-DD")
            return 1
        persist_engine_freeze_iso(
            workspace,
            engine_name=name,
            freeze_iso=freeze_iso,
            actor="operator",
            source="cli",
            reason=reason,
        )
        print(f"engine_frozen: {name} until {freeze_iso}" + (f" reason={reason}" if reason else ""))
        return 0
    if not clear_persisted_engine_freeze(
        workspace,
        engine_name=name,
        actor="operator",
        source="cli",
        reason=reason,
    ):
        print(f"engine unfreeze: {name} is not frozen")
        return 1
    print(f"engine_unfrozen: {name}")
    return 0


def _parse_engine_preference(value: str | None) -> list[str] | None:
    if value is None:
        return None
    engines = [part.strip() for part in value.replace(",", " ").split() if part.strip()]
    if not engines:
        return None
    return normalize_engine_sequence(engines, field_name="engine_preference")


def _engine_list_label(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def _render_engine_audit_lines(root: Path, key: str | None, limit: int) -> list[str]:
    entries = load_runtime_setting_audit_entries(root, key=key, limit=limit)
    lines = [f"setting_audit_entries: {len(entries)}"]
    for entry in entries:
        lines.extend(
            [
                f"id: {entry.id}",
                f"key: {entry.key}",
                f"created_at: {entry.created_at}",
                f"actor: {entry.actor}",
                f"source: {entry.source}",
                f"old_value: {entry.old_value}",
                f"new_value: {entry.new_value}",
                f"context: {entry.context}",
            ]
        )
    return lines


def _render_engine_status_lines(root: Path) -> list[str]:
    config = load_config(root)
    active_freezes = active_engine_freezes(config)
    quota_statuses = _collect_quota_statuses()
    lines = [
        f"default_engine: {config.default_engine}",
        f"engine_preference: {','.join(config.engine_preference) if config.engine_preference else '-'}",
        f"engine_freeze: {', '.join(f'{k}={v}' for k, v in sorted(config.engine_freeze.items())) or '-'}",
    ]
    for engine_name in ENGINE_CHOICES:
        caps = get_engine(engine_name).capabilities
        frozen_until = config.engine_freeze.get(engine_name) if engine_name in active_freezes else None
        lines.extend(
            [
                "",
                (
                    f"engine: {engine_name} "
                    f"available={'yes' if caps.available else 'no'} "
                    f"frozen={'yes' if frozen_until else 'no'} "
                    f"frozen_until={frozen_until or '-'}"
                ),
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


def _render_quota_line(_engine_name: str, status: object) -> str:
    quota_error = _quota_status_error(status)
    if quota_error is not None:
        return quota_error
    if getattr(status, "short_term", None) is None or getattr(status, "long_term", None) is None:
        return "quota: unavailable (unsupported usage shape)"
    if bool(getattr(status, "limit_reached", False)):
        return "quota: limited"
    return "quota: ok"


def _quota_status_error(status: object) -> str | None:
    if status == "unsupported":
        return "quota: unsupported"
    error = getattr(status, "error", None)
    if error is not None:
        return f"quota: unavailable ({error})"
    return None
