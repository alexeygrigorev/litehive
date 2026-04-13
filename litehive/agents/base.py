"""Compatibility shim for the extracted heru adapter contract."""

from heru.base import *  # noqa: F403
from heru.base import parse_stage_report_text as _heru_parse_stage_report_text


def parse_stage_report_text(*args, **kwargs):  # type: ignore[no-untyped-def]
    report = _heru_parse_stage_report_text(*args, **kwargs)
    report.warnings = [
        warning.replace("litehive agent report CLI", "litehive report CLI")
        for warning in report.warnings
    ]
    return report

