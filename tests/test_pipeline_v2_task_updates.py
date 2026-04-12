"""Tests for TASK_UPDATE application from grooming verdict comments.

Exercises both paths that ``apply_task_updates_from_comment`` has to
handle: (1) a structured YAML block the planner embeds in its message,
and (2) plain-text section extraction as a fallback.
"""

from pathlib import Path

import pytest

from litehive.pipeline.task_updates import (
    extract_yaml_block,
    apply_task_updates_from_comment,
)
from litehive.tasks.crud import create_task, get_task

from tests.workspace_helpers import ensure_workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ensure_workspace(tmp_path)
    return tmp_path


def test_extract_yaml_block_pulls_task_update_section() -> None:
    msg = """Verdict: pass.

TASK_UPDATE:
goal: Build the thing
acceptance_criteria:
  - First criterion
  - Second criterion

Summary: done."""
    parsed = extract_yaml_block(msg)
    assert parsed is not None
    assert parsed["goal"] == "Build the thing"
    assert parsed["acceptance_criteria"] == ["First criterion", "Second criterion"]


def test_apply_task_updates_from_comment_updates_task_record(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="placeholder")

    message = """Grooming pass.

TASK_UPDATE:
goal: Sharpened goal statement
acceptance_criteria:
  - The tool prints ok
  - Exit code is zero
plan:
  - Step one
  - Step two
"""

    applied = apply_task_updates_from_comment(workspace, task, message=message)
    assert applied is True

    reloaded = get_task(workspace, task.id)
    assert reloaded.goal == "Sharpened goal statement"
    assert reloaded.acceptance_criteria == ["The tool prints ok", "Exit code is zero"]
    assert reloaded.plan == ["Step one", "Step two"]
