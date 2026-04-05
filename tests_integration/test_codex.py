import pytest

from litehive.tasks import create_task, load_task_thread, require_task, set_active_task

from .helpers import (
    execute_engine_prompt,
    extract_stage_result_submission,
    require_real_engine,
    smoke_prompt,
)


pytestmark = pytest.mark.integration


def test_codex_emits_structured_stage_result(integration_root) -> None:
    require_real_engine("codex")
    engine, execution = execute_engine_prompt(
        "codex",
        prompt=smoke_prompt("codex"),
        cwd=integration_root,
    )
    assert execution.exit_code == 0, execution.transcript
    transcript = engine.render_transcript(execution)
    submission = extract_stage_result_submission(transcript)
    assert submission.summary == "codex integration smoke"
    report = engine.parse_stage_report(
        task_id="T-INTEGRATION",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )
    assert report.verdict == "pass"
    assert report.summary == submission.summary


def test_codex_can_invoke_litehive_report_and_persist_thread_comment(integration_root) -> None:
    require_real_engine("codex")
    task = create_task(integration_root, title="Integration report task", auto_commit=False)
    set_active_task(integration_root, task.id)
    prompt = (
        "Run this shell command exactly once and wait for it to succeed:\n"
        f"`litehive report --verdict pass --role swe --step implementing --task-id {task.id} "
        f'--workspace {integration_root} --message "integration report from codex"`\n'
        "After it succeeds, reply with exactly this and nothing else:\n"
        "SUMMARY: report invoked\n"
        "STAGE_RESULT:\n"
        '{"verdict":"pass","summary":"report invoked","files_changed":[],"tests":{"added":0,"passing":0},"warnings":[],"follow_up_tasks":[],"acceptance_criteria":[]}'
    )
    engine, execution = execute_engine_prompt("codex", prompt=prompt, cwd=integration_root)
    assert execution.exit_code == 0, execution.transcript
    submission = extract_stage_result_submission(engine.render_transcript(execution))
    assert submission.summary == "report invoked"
    thread = load_task_thread(integration_root, require_task(integration_root, task.id))
    assert thread[-1].role == "swe"
    assert thread[-1].step == "implementing"
    assert thread[-1].verdict == "pass"
    assert thread[-1].message == "integration report from codex"
