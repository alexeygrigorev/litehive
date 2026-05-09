"""
Engine freeze state and audited persistence helpers.

Keeps freeze parsing, active-freeze projection, and runtime-settings
writes together so engine selection can consume freeze facts without
owning the storage details.
"""

from datetime import datetime, timezone

from litehive.config.model import LitehiveConfig
from litehive.config.time_parsing import parse_utc_datetime


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
