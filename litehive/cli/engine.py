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
from litehive.config.model import LitehiveConfig
from litehive.config.model import normalize_engine_sequence
from litehive.config.runtime_settings import (
    load_runtime_setting_audit_entries,
    set_default_engine,
    set_engine_preference,
)
from litehive.container import build_container
from litehive.workspace import Workspace


def engine_command(
    action: Annotated[
        str,
        typer.Argument(
            click_type=choice(["audit", "default", "freeze", "preference", "status", "unfreeze"]),
            help="Subcommand",
        ),
    ],
    workspace: WorkspaceOption = Path.cwd(),
    name: Annotated[str | None, typer.Argument(help="Engine name, setting key, or comma-separated engine list")] = None,
    until: Annotated[str | None, typer.Option(help="Freeze until this ISO date (YYYY-MM-DD)")] = None,
    reason: Annotated[str | None, typer.Option(help="Operator note")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum audit rows to show")] = 20,
) -> int:
    """
    Operator entrypoint behind ``litehive engine``.

    Routes the chosen subcommand to status, audit, default,
    preference, freeze, or unfreeze. Uses one positional ``action``
    rather than separate Typer commands so the engine-name argument
    can be reused across subcommands without exposing six near-
    duplicate command signatures.
    """
    container = build_container(workspace)
    root = container.workspace.root
    if action == "status":
        if name:
            print("engine status: does not take positional arguments")
            return 1
        for line in _render_engine_status_lines(container.config):
            print(line)
        return 0
    if action == "audit":
        for line in _render_engine_audit_lines(container.workspace, key=name, limit=limit):
            print(line)
        return 0
    if action == "default":
        if name is None or name not in ENGINE_CHOICES:
            print(f"engine default: unknown engine '{name}'")
            return 1
        if reason:
            default_context = {"reason": reason}
        else:
            default_context = None
        change = set_default_engine(
            container.workspace,
            name,
            actor="operator",
            source="cli",
            context=default_context,
        )
        print(f"default_engine: {change.old_value} -> {change.new_value}")
        if change.changed:
            updated_label = "yes"
        else:
            updated_label = "no"
        print(f"updated: {updated_label}")
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
        if reason:
            preference_context = {"reason": reason}
        else:
            preference_context = None
        try:
            change = set_engine_preference(
                container.workspace,
                preference,
                actor="operator",
                source="cli",
                context=preference_context,
            )
        except ValueError as exc:
            print(f"engine preference: {exc}")
            return 1
        print(f"engine_preference: {_engine_list_label(change.old_value)} -> {_engine_list_label(change.new_value)}")
        if change.changed:
            updated_label = "yes"
        else:
            updated_label = "no"
        print(f"updated: {updated_label}")
        return 0
    if name is None or name not in ENGINE_CHOICES:
        print(f"engine {action}: unknown engine '{name}'")
        return 1
    if action == "freeze":
        freeze_iso = parse_engine_freeze_until(until)
        if freeze_iso is None:
            print("engine freeze: --until must be ISO date YYYY-MM-DD")
            return 1
        persist_engine_freeze_iso(
            root,
            engine_name=name,
            freeze_iso=freeze_iso,
            actor="operator",
            source="cli",
            reason=reason,
        )
        if reason:
            reason_part = f" reason={reason}"
        else:
            reason_part = ""
        print(f"engine_frozen: {name} until {freeze_iso}" + reason_part)
        return 0
    if not clear_persisted_engine_freeze(
        root,
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
    """
    Parse an operator-supplied preference string into a normalized engine list.

    Accepts comma- or space-separated names because the operator may
    type either form on the shell. Normalization through
    :func:`normalize_engine_sequence` enforces the canonical names
    and order before persistence so the audit log records a clean
    value, not whatever the operator typed.
    """
    if value is None:
        return None
    engines = _split_engine_preference_string(value)
    if not engines:
        return None
    return normalize_engine_sequence(engines, field_name="engine_preference")


def _split_engine_preference_string(value: str) -> list[str]:
    """
    Split an operator-typed engine list into trimmed names.

    The operator may type ``"claude, codex"`` or ``"claude codex"``,
    so commas are first folded into spaces before splitting; empty
    fragments (from doubled separators) are dropped. Returned to
    :func:`_parse_engine_preference` for canonical normalization.
    """
    flattened = value.replace(",", " ")
    raw_parts = flattened.split()
    engines: list[str] = []
    for part in raw_parts:
        trimmed = part.strip()
        if trimmed:
            engines.append(trimmed)
    return engines


def _engine_list_label(value: object) -> str:
    """
    Render an audit-row engine value uniformly across shapes.

    The persisted value can be a list (preference) or a scalar
    (legacy default). Rather than branch in the caller, this helper
    always returns the comma-joined or stringified form so the
    "before -> after" line in ``engine preference`` output looks the
    same regardless of which historical shape was read back.
    """
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def _render_engine_audit_lines(workspace: Workspace, key: str | None, limit: int) -> list[str]:
    """
    Render the audit log block shown by ``engine audit``.

    Pulls runtime-setting audit entries from the SQLite store and
    flattens each into a fixed line block (id, key, timestamps,
    actor, source, before/after values, context). Operators read
    this output to reconstruct who changed an engine setting and
    why, so the layout is intentionally script-friendly.
    """
    entries = load_runtime_setting_audit_entries(workspace, key=key, limit=limit)
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


def _render_engine_status_lines(config: LitehiveConfig) -> list[str]:
    """
    Build the per-engine status block shown by ``engine status``.

    Combines workspace config (default engine, preference order,
    persisted freezes), live engine capabilities (available?), and
    one quota probe per provider. The operator uses this to decide
    whether to flip the default engine before queueing more work.
    """
    active_freezes = active_engine_freezes(config)
    quota_statuses = _collect_quota_statuses()
    if config.engine_preference:
        engine_preference_label = ",".join(config.engine_preference)
    else:
        engine_preference_label = "-"
    lines = [
        f"default_engine: {config.default_engine}",
        f"engine_preference: {engine_preference_label}",
        f"engine_freeze: {_engine_freeze_summary_line(config.engine_freeze)}",
    ]
    for engine_name in ENGINE_CHOICES:
        caps = get_engine(engine_name).capabilities
        if engine_name in active_freezes:
            frozen_until = config.engine_freeze.get(engine_name)
        else:
            frozen_until = None
        if caps.available:
            available_label = "yes"
        else:
            available_label = "no"
        if frozen_until:
            frozen_label = "yes"
        else:
            frozen_label = "no"
        lines.extend(
            [
                "",
                (
                    f"engine: {engine_name} "
                    f"available={available_label} "
                    f"frozen={frozen_label} "
                    f"frozen_until={frozen_until or '-'}"
                ),
                _render_quota_line(engine_name, quota_statuses[engine_name]),
            ]
        )
    return lines


def _engine_freeze_summary_line(engine_freeze: dict[str, str]) -> str:
    """
    Format the persisted engine-freeze map for ``engine status``.

    Returns ``"-"`` when nothing is frozen so the operator sees an
    obvious placeholder; otherwise emits ``engine=until`` pairs in
    sorted order so consecutive runs of ``engine status`` are
    diffable. Caller: :func:`_render_engine_status_lines`.
    """
    if not engine_freeze:
        return "-"
    sorted_items = sorted(engine_freeze.items())
    pairs: list[str] = []
    for engine_name, until in sorted_items:
        pairs.append(f"{engine_name}={until}")
    return ", ".join(pairs)


def _collect_quota_statuses() -> dict[str, object]:
    """
    Probe each supported provider's quota once.

    Returns a dict keyed by engine name so the status renderer can
    do an O(1) lookup per row instead of paying the network cost
    per engine. Z.ai's probe covers both ``goz`` and ``opencode``
    because they share a backend; ``gemini`` has no probe surface
    so it carries the literal ``"unsupported"`` sentinel.
    """
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
    """
    Run a quota probe under a broad except.

    A single broken provider must not blank out the whole status
    table; the operator still wants to see the engines that *do*
    work. The exception is converted into a labeled
    :class:`UsageStatus` so downstream rendering can treat it the
    same as a successful probe with an error attribute.
    """
    try:
        return checker()
    except Exception as exc:  # pragma: no cover - defensive fallback
        return UsageStatus(error=_quota_error_label(exc))


def _quota_error_label(exc: Exception) -> str:
    """
    Pick a human-readable label for a failed quota probe.

    Some provider SDKs raise with empty ``str(exc)`` (e.g. wrapped
    HTTP errors). Falling back to the exception class name keeps
    the status row informative instead of printing
    ``quota: unavailable ()`` which would look broken.
    """
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def _render_quota_line(_engine_name: str, status: object) -> str:
    """
    Translate a heru quota probe result into one ``quota: ...`` row.

    Renders the limited / ok / unavailable verdict the operator
    sees in ``engine status``. The engine name is accepted but
    unused — kept on the signature so callers do not have to
    reshape the call site if a future provider needs per-engine
    formatting.
    """
    quota_error = _quota_status_error(status)
    if quota_error is not None:
        return quota_error
    if getattr(status, "short_term", None) is None or getattr(status, "long_term", None) is None:
        return "quota: unavailable (unsupported usage shape)"
    if bool(getattr(status, "limit_reached", False)):
        return "quota: limited"
    return "quota: ok"


def _quota_status_error(status: object) -> str | None:
    """
    Detect the unsupported/error variants of a quota probe result.

    Returns a pre-formatted ``quota: ...`` string when the status
    is the ``"unsupported"`` sentinel or carries an ``error``
    attribute; returns ``None`` on the happy path so
    :func:`_render_quota_line` continues with the limit check.
    """
    if status == "unsupported":
        return "quota: unsupported"
    error = getattr(status, "error", None)
    if error is not None:
        return f"quota: unavailable ({error})"
    return None
