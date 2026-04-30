from pathlib import Path

from litehive.agents.prompts import stage_prompt
from litehive.config.workspace import ensure_workspace
from litehive.roles.planner import INSTRUCTIONS as PLANNER_INSTRUCTIONS
from litehive.roles.reviewer import ROLE_GUIDANCE as REVIEWER_GUIDANCE
from litehive.state.records import create_task


def test_stage_prompt_only_advertises_pass_and_reject_for_non_recovery_agents(tmp_path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Prompt verdict contract", goal="shape the task")

    text = stage_prompt(task, "grooming", root=tmp_path)

    assert 'litehive report --verdict pass --message "your report text"' in text
    assert "Your allowed verdicts are <pass|reject>." in text
    assert "--verdict blocked" not in text


def test_grooming_prompt_requires_current_main_preflight_before_planning(tmp_path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Preflight already landed work", goal="avoid redundant planning")

    text = stage_prompt(task, "grooming", root=tmp_path)

    preflight_idx = text.index("run a grooming preflight against current `main` and recent landed work")
    planning_idx = text.index("Frame the real user problem")
    assert preflight_idx < planning_idx
    assert "`litehive agent close --outcome done --reason ...`" in text
    assert "`litehive agent close --outcome duplicate --reason ...`" in text
    assert "`litehive task close <task-id>" not in text


def test_pipeline_monitor_prompt_does_not_advertise_blocked_for_swe() -> None:
    text = Path("prompts/pipeline-monitor.md").read_text(encoding="utf-8")

    assert "pass/blocked" not in text
    assert "pass|blocked" not in text
    assert "pass/reject only" in text


def test_role_prompts_describe_duplicate_wont_do_deferred_as_close_reasons() -> None:
    combined = f"{PLANNER_INSTRUCTIONS}\n{REVIEWER_GUIDANCE}"
    stale_outcome_placeholder = "--outcome <" + "status>"
    stale_closed_status_phrase = "special closed " + "status"

    assert "These outcomes are close reasons, not task statuses." in PLANNER_INSTRUCTIONS
    assert "close reasons `wont_do`, `duplicate`, or `deferred`" in REVIEWER_GUIDANCE
    assert stale_outcome_placeholder not in combined
    assert stale_closed_status_phrase not in combined
