"""Compatibility shim for the extracted Claude adapter."""

from heru.adapters.claude import *
from heru.adapters.claude import (
    _extract_claude_text_delta_fallback as _extract_claude_text_delta_fallback,
)
