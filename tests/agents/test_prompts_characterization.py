"""Characterization tests for ``litehive.agents.prompts.stage_prompt``.

Per docs/feedback-2026-05-03.md (R11): "Before any non-trivial
refactor, the affected behavior must be covered by tests."

The PromptBuilder restructure (task #20) wants to drop several
prompt fields the reviewer flagged as cryptic / useless and
replace the dict-shaped scaffolding with typed inputs. These tests
pin the *structural* shape of the rendered prompt — the section
headers, the verdict-instruction block, the activity log
inclusion, the stage role line — without freezing wording inside
each section. So a refactor that keeps the contract intact (same
sections, same fields populated when present) passes; a refactor
that drops a section we still depend on fails.

The tests deliberately do NOT assert exact prose for sections like
``Stage instructions``: that's profile-driven and changes are
editorial, not structural.
"""

from pathlib import Path

import pytest

from litehive.agents.prompts import stage_prompt
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task


_REQUIRED_SECTION_HEADERS = [
    "Task:",
    "Stage:",
    "Stage owner:",
    "Process profile:",
    "Workspace context:",
    "Shared process:",
    "Project overlay:",
    "Prompt scaffold:",
    "Role focus:",
    "Stage instructions:",
    "Goal:",
    "Acceptance criteria:",
    "Plan:",
    "Constraints:",
]


@pytest.mark.parametrize(
    "stage,expected_owner",
    [
        ("grooming", "planner"),
        ("implementing", "swe"),
        ("testing", "qa"),
        ("accepting", "reviewer"),
    ],
)
def test_stage_prompt_emits_all_required_sections(tmp_path: Path, stage: str, expected_owner: str) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Characterize prompt shape",
        goal="Surface the contract this prompt satisfies",
        acceptance_criteria=["The prompt has every required section"],
    )

    text = stage_prompt(task, stage, root=tmp_path)

    assert f"Task: {task.id} {task.title}" in text
    assert f"Stage: {stage}" in text
    assert f"Stage owner: {expected_owner}" in text
    for header in _REQUIRED_SECTION_HEADERS:
        assert header in text, f"missing required section header `{header}` in {stage} prompt"


def test_stage_prompt_emits_verdict_submission_instructions(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Verdict instructions", goal="check report block")

    text = stage_prompt(task, "implementing", root=tmp_path)

    assert "litehive agent report --verdict pass" in text
    assert "Your allowed verdicts are <pass|reject>." in text
    assert "Report requirements:" in text
    assert "On PASS:" in text
    assert "On REJECT:" in text


def test_recovery_stage_prompt_advertises_recovery_verdict_set(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery verdicts", goal="check recovery contract")

    text = stage_prompt(task, "recovering", root=tmp_path, role_name="recovery")

    assert "Your allowed verdicts are <resume|advance|done|budget_hit|reject>." in text
    assert "litehive agent report --verdict resume" in text


def test_stage_prompt_drops_task_type_line(tmp_path: Path) -> None:
    """Per docs/feedback-2026-05-03.md: task_type was useless info; the
    prompt must not surface it. Pinning this so a future restore
    fails loudly."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="No task_type", goal="confirm absence")

    text = stage_prompt(task, "implementing", root=tmp_path)

    assert "Task type:" not in text


def test_stage_prompt_omits_acceptance_gate_when_criteria_present(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="With criteria",
        goal="check gate omission",
        acceptance_criteria=["A specific verifiable outcome"],
    )

    text = stage_prompt(task, "implementing", root=tmp_path)

    assert "Acceptance gate:" not in text


def test_grooming_prompt_includes_acceptance_criteria_best_practices(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Best practices", goal="check best-practices block")

    text = stage_prompt(task, "grooming", root=tmp_path)

    assert "Acceptance criteria best practices:" in text
    assert "observable outcome" in text


def test_single_mode_implementing_prompt_emits_single_mode_overlay(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Single mode overlay", goal="check overlay", pipeline_mode="single")

    text = stage_prompt(task, "implementing", root=tmp_path)

    assert "Pipeline mode is `single`" in text


def test_full_mode_implementing_prompt_does_not_emit_single_mode_overlay(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Full mode no overlay", goal="check absence")

    text = stage_prompt(task, "implementing", root=tmp_path)

    assert "Pipeline mode is `single`" not in text


def test_stage_prompt_uses_LITEHIVE_TASK_ID_environment_hint(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Env hint", goal="check env block")

    text = stage_prompt(task, "implementing", root=tmp_path)

    assert f"LITEHIVE_TASK_ID is set to {task.id}" in text
