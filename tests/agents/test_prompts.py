from pathlib import Path

from litehive.roles.planner import INSTRUCTIONS as PLANNER_INSTRUCTIONS
from litehive.roles.reviewer import ROLE_GUIDANCE as REVIEWER_GUIDANCE


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
