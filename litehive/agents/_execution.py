"""Backward-compatible subagent execution module alias."""

from litehive.agents import get_engine
from litehive.agents import _manager as _manager_module
from litehive.agents._manager import SubagentManager as _SubagentManager


class SubagentManager(_SubagentManager):
    """Compatibility wrapper that preserves legacy module monkeypatch points."""

    def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_get_engine = _manager_module.get_engine
        _manager_module.get_engine = get_engine
        try:
            return super().run(*args, **kwargs)
        finally:
            _manager_module.get_engine = original_get_engine


__all__ = ["SubagentManager", "get_engine"]
