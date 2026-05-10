from pathlib import Path
from typing import Annotated

import typer
from heru import ENGINE_CHOICES, get_engine

from litehive.cli.common import WorkspaceOption, choice
from litehive.config.engine_freezes import (
    active_engine_freezes,
)
from litehive.config.engine_models import EngineRoutingPolicy, parse_engine_freeze_until
from litehive.config.engine_quota import collect_engine_quota_statuses
from litehive.config.model import LitehiveConfig
from litehive.config.model import normalize_engine_sequence
from litehive.config.runtime_settings import RuntimeSettingsRepository
from litehive.container import build_container, build_workspace
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
    if action == "status":
        return _engine_status_command(workspace, name)
    workspace_obj = build_workspace(workspace)
    if action == "audit":
        return _engine_audit_command(workspace_obj, name, limit)
    if action == "default":
        return _engine_default_command(workspace_obj, name, reason)
    if action == "preference":
        return _engine_preference_command(workspace_obj, name, reason)
    if name is None or name not in ENGINE_CHOICES:
        print(f"engine {action}: unknown engine '{name}'")
        return 1
    if action == "freeze":
        return _engine_freeze_command(workspace_obj, name, until, reason)
    return _engine_unfreeze_command(workspace_obj, name, reason)


def _engine_status_command(workspace: Path, name: str | None) -> int:
    """
    Render the multi-engine status block for ``engine status``.

    Refuses a positional engine name because the status view always
    shows every configured engine; filtering is handled by the
    audit subcommand instead.
    """
    if name:
        print("engine status: does not take positional arguments")
        return 1
    container = build_container(workspace)
    for line in _render_engine_status_lines(container.config):
        print(line)
    return 0


def _engine_audit_command(workspace: Workspace, key: str | None, limit: int) -> int:
    """
    Print runtime-setting audit entries for ``engine audit``.

    Delegates to :func:`_render_engine_audit_lines` and emits the
    result line by line so the function stays a thin command wrapper
    that tests can call without going through Typer.
    """
    for line in _render_engine_audit_lines(workspace, key=key, limit=limit):
        print(line)
    return 0


def _engine_default_command(workspace: Workspace, name: str | None, reason: str | None) -> int:
    """
    Set the workspace's default engine via ``engine default``.

    Persists the change through :class:`EngineRoutingPolicy` so the
    audit trail records the old and new value. Prints both values
    and whether the setting actually changed so the operator can
    confirm the write landed.
    """
    if name is None or name not in ENGINE_CHOICES:
        print(f"engine default: unknown engine '{name}'")
        return 1
    config = workspace.load_config()
    old_value = config.default_engine
    EngineRoutingPolicy(workspace, config).set_default(name, reason=reason)
    print(f"default_engine: {old_value} -> {name}")
    print(f"updated: {_updated_label(old_value != name)}")
    return 0


def _engine_preference_command(workspace: Workspace, name: str | None, reason: str | None) -> int:
    """
    Set the engine preference order via ``engine preference``.

    Parses the operator-supplied comma-separated list, normalises it
    through the canonical engine registry, and persists the change
    with audit attribution. Prints old and new preference strings so
    the operator can verify the write.
    """
    try:
        preference = _parse_engine_preference(name)
    except ValueError as exc:
        print(f"engine preference: {exc}")
        return 1
    if preference is None:
        print("engine preference: provide a comma-separated engine list")
        return 1
    try:
        config = workspace.load_config()
        old_value = list(config.engine_preference)
        EngineRoutingPolicy(workspace, config).set_preference(preference, reason=reason)
    except ValueError as exc:
        print(f"engine preference: {exc}")
        return 1
    print(f"engine_preference: {_engine_list_label(old_value)} -> {_engine_list_label(preference)}")
    print(f"updated: {_updated_label(old_value != preference)}")
    return 0


def _engine_freeze_command(workspace: Workspace, name: str, until: str | None, reason: str | None) -> int:
    """
    Freeze an engine until a given date via ``engine freeze``.

    Parses the ``--until`` ISO date, persists the freeze through
    the routing policy, and prints a confirmation line with the
    effective date and optional reason.
    """
    freeze_iso = parse_engine_freeze_until(until)
    if freeze_iso is None:
        print("engine freeze: --until must be ISO date YYYY-MM-DD")
        return 1
    EngineRoutingPolicy(workspace, workspace.load_config()).freeze(name, until=freeze_iso, reason=reason)
    if reason:
        reason_part = f" reason={reason}"
    else:
        reason_part = ""
    print(f"engine_frozen: {name} until {freeze_iso}" + reason_part)
    return 0


def _engine_unfreeze_command(workspace: Workspace, name: str, reason: str | None) -> int:
    """
    Remove an engine freeze via ``engine unfreeze``.

    Returns an error when the engine was not frozen so the operator
    can distinguish "nothing to do" from a successful unfreeze.
    """
    unfrozen = EngineRoutingPolicy(workspace, workspace.load_config()).unfreeze(name, reason=reason)
    if not unfrozen:
        print(f"engine unfreeze: {name} is not frozen")
        return 1
    print(f"engine_unfrozen: {name}")
    return 0


def _updated_label(changed: bool) -> str:
    """
    Render the ``updated`` boolean as ``"yes"`` / ``"no"`` for the
    engine subcommand output.
    """
    if changed:
        return "yes"
    return "no"


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
    entries = RuntimeSettingsRepository(workspace).audit_entries(key=key, limit=limit)
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
    quota_statuses = collect_engine_quota_statuses()
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
