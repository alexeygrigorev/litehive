"""Apply grooming-stage TASK_UPDATE changes from an agent's verdict comment.

Grooming agents can emit either:

1. A structured ``TASK_UPDATE:`` YAML block in their report message, or
2. Plain-text ``ACCEPTANCE_CRITERIA: / PLAN: / CONSTRAINTS: /
   PM_COMPLEXITY: / PLANNED_EFFORT:`` sections.

Both paths funnel into ``litehive.workspace.workflow.apply_task_updates_from_report``,
which expects a ``StageReport``-shaped object with ``task_update`` (dict)
and ``feedback`` (text) fields.

Agents submit via ``litehive report``, which writes a ``TaskThreadComment``
with only a plain ``message`` string — no structured ``task_update``
field. So this module constructs a shim object carrying the comment's
message as ``feedback`` (letting the text-section extractors run) and
passes it through. If the comment's message happens to contain a YAML
``TASK_UPDATE:`` block, we parse it and set ``task_update`` on the
shim so the structured path runs.

Called from ``HeruEngineAdapter.run_turn`` after the verdict reader
detects a fresh agent submission.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from litehive.models import TaskRecord


_TASK_UPDATE_BLOCK = re.compile(
    r"^TASK_UPDATE:\s*\n(.*?)(?=\n\s*\n|\Z)",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class _ReportShim:
    """Minimal duck-typed object that ``apply_task_updates_from_report`` reads."""

    task_update: dict[str, Any] | None = None
    feedback: str = ""
    files_changed: list[str] = field(default_factory=list)
    tests: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    summary: str = ""
    verdict: str = "pass"


def extract_yaml_block(message: str) -> dict[str, Any] | None:
    """Pull a ``TASK_UPDATE:`` YAML block out of the message if present."""
    if not message:
        return None
    match = _TASK_UPDATE_BLOCK.search(message)
    if not match:
        return None
    body = match.group(1).strip()
    if not body:
        return None
    try:
        parsed = yaml.safe_load(body)
    except yaml.YAMLError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def apply_task_updates_from_comment(
    workspace_root: Path,
    task: TaskRecord,
    *,
    message: str,
) -> bool:
    """Apply any task field updates embedded in an agent's verdict message.

    Returns True if anything was applied, False otherwise. Errors from
    the underlying apply path are swallowed and logged to stderr —
    a malformed TASK_UPDATE shouldn't fail the pipeline.
    """
    from litehive.workspace.workflow import apply_task_updates_from_report

    shim = _ReportShim(
        task_update=extract_yaml_block(message),
        feedback=message or "",
    )
    try:
        return apply_task_updates_from_report(workspace_root, task, shim)
    except Exception as exc:
        import sys

        print(
            f"[task_updates] ignored error applying task update: {exc}",
            file=sys.stderr,
        )
        return False
