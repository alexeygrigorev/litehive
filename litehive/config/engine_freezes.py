"""
Engine freeze state and audited persistence helpers.

Keeps freeze parsing, active-freeze projection, and runtime-settings
writes together so engine selection can consume freeze facts without
owning the storage details.
"""

from datetime import datetime, timezone

from litehive.config.model import LitehiveConfig
from litehive.config.runtime_settings import RuntimeSettingContext, clear_engine_freeze, set_engine_freeze
from litehive.config.time_parsing import parse_utc_datetime
from litehive.workspace import Workspace


def is_engine_frozen(config: LitehiveConfig, engine_name: str) -> bool:
    """
    Report whether an engine is currently frozen.

    Frozen means the freeze datetime is still in the future; an
    expired freeze is treated as not-frozen so engine selection
    self-cleans on the next pass without an explicit sweeper.
    """
    freeze_dt = parse_utc_datetime(config.engine_freeze.get(engine_name))
    if freeze_dt is None:
        return False
    return datetime.now(timezone.utc) < freeze_dt


def active_engine_freezes(config: LitehiveConfig) -> dict[str, datetime]:
    """
    Return currently-active freezes as ``{engine: freeze_utc_datetime}``.

    Used by status renderers and engine selection to decide which
    engines are off the table right now. Drops expired entries
    silently because an expired freeze should not appear "active"
    to operator-facing output.
    """
    now = datetime.now(timezone.utc)
    result: dict[str, datetime] = {}
    for engine_name, freeze_str in config.engine_freeze.items():
        freeze_dt = parse_utc_datetime(freeze_str)
        if freeze_dt is None:
            continue
        if now < freeze_dt:
            result[engine_name] = freeze_dt
    return result


def persist_engine_freeze_iso_for_workspace(
    workspace: Workspace,
    engine_name: str,
    freeze_iso: str,
    actor: str = "system",
    source: str = "runtime",
    reason: str | None = None,
) -> None:
    """
    Write a freeze entry through an injected workspace.

    Called by the CLI ``engine freeze`` command and by
    quota-driven freezes inside engine selection. Both paths
    funnel through here so audit rows for an operator-typed
    freeze and a quota-detected freeze share the same shape and
    can be compared directly.
    """
    if reason:
        context: RuntimeSettingContext | None = {"reason": reason}
    else:
        context = None
    set_engine_freeze(
        workspace,
        engine_name=engine_name,
        freeze_iso=freeze_iso,
        actor=actor,
        source=source,
        context=context,
    )


def clear_persisted_engine_freeze_for_workspace(
    workspace: Workspace,
    engine_name: str,
    actor: str = "system",
    source: str = "runtime",
    reason: str | None = None,
) -> bool:
    """
    Remove a freeze entry through an injected workspace.

    Returns whether anything actually changed so callers can
    avoid emitting "unfroze nothing" log lines. Called by the
    CLI ``engine unfreeze`` command and by engine selection when
    a previously-frozen engine's freeze window has expired —
    same audit path as the freeze write so the audit log
    captures both directions.
    """
    if reason:
        context: RuntimeSettingContext | None = {"reason": reason}
    else:
        context = None
    return clear_engine_freeze(
        workspace,
        engine_name=engine_name,
        actor=actor,
        source=source,
        context=context,
    ).changed
