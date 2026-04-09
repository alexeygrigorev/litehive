"""Backward-compatible alias for pipeline builder helpers."""

import sys

from litehive.pipeline import _builder as _pipeline_builder

sys.modules[__name__] = _pipeline_builder
