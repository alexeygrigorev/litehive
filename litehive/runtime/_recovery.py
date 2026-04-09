"""Backward-compatible alias for pipeline recovery helpers."""

import sys

from litehive.pipeline import _recovery as _pipeline_recovery

sys.modules[__name__] = _pipeline_recovery
