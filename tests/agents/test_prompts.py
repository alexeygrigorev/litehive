from pathlib import Path

from litehive.agents.prompts import stage_prompt
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task


def test_stage_prompt_only_advertises_pass_and_reject_for_non_recovery_agents(tmp_path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Prompt verdict contract", goal="shape the task")

    text = stage_prompt(task, "grooming", root=tmp_path)

    assert 'litehive report --verdict pass --role planner --message "your report text"' in text
    assert "Your allowed verdicts are <pass|reject>." in text
    assert "--verdict blocked" not in text


def test_pipeline_monitor_prompt_does_not_advertise_blocked_for_swe() -> None:
    text = Path("prompts/pipeline-monitor.md").read_text(encoding="utf-8")

    assert "pass/blocked" not in text
    assert "pass|blocked" not in text
    assert "pass/reject only" in text
