"""Backward-compatible module alias for legacy subagent manager imports."""

import sys

from litehive.agents import _manager as _manager_module

sys.modules[__name__] = _manager_module
