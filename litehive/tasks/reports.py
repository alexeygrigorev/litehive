"""Compatibility facade for task report APIs.

Concrete ownership lives in:

- ``activity_rendering`` for activity display, file normalization, and retraction.
- ``recovery_evidence`` for recovery evidence and artifact lookup.
- ``recovery_reports`` for recovery report construction.
- ``report_storage`` for SQLite-backed report persistence and lookup.
"""

from litehive.tasks.activity_rendering import (
    RETRACTED_FILESYSTEM_MARKER,
    append_activity_entry,
    is_retractable_pass_entry,
    is_retracted_activity_entry,
    normalized_files_changed,
    retract_activity_entry,
    render_task_activity,
)
from litehive.tasks.recovery_evidence import collect_recovery_evidence
from litehive.tasks.recovery_reports import record_recovery_report
from litehive.tasks.report_storage import (
    ReportReference,
    latest_recovery_report,
    latest_stage_report,
    load_recovery_reports,
    load_stage_reports,
    load_stage_reports_for_task_id,
    load_workspace_stage_reports,
    record_stage_report,
    rewrite_latest_stage_report,
)

__all__ = [
    "RETRACTED_FILESYSTEM_MARKER",
    "ReportReference",
    "append_activity_entry",
    "collect_recovery_evidence",
    "is_retractable_pass_entry",
    "is_retracted_activity_entry",
    "latest_recovery_report",
    "latest_stage_report",
    "load_recovery_reports",
    "load_stage_reports",
    "load_stage_reports_for_task_id",
    "load_workspace_stage_reports",
    "normalized_files_changed",
    "record_recovery_report",
    "record_stage_report",
    "retract_activity_entry",
    "render_task_activity",
    "rewrite_latest_stage_report",
]
