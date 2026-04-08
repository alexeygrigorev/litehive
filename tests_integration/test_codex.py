import json

import pytest

from litehive.engines import extract_engine_continuation
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
        '{"verdict":"pass","summary":"report invoked","files_changed":[],"tests":{"added":0,"passing":0},"warnings":[],"acceptance_criteria":[]}'
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


def test_codex_session_resume_via_exec_with_thread_context(integration_root) -> None:
    """Run codex exec, extract thread_id, then run again with session context.

    This verifies the nudge flow: after an agent finishes without submitting
    a verdict, we start a new codex exec with the session context prepended
    to the prompt. The resumed session should complete successfully.
    """
    require_real_engine("codex")

    # Step 1: initial run — get a thread_id
    engine, first_run = execute_engine_prompt(
        "codex",
        prompt=smoke_prompt("codex"),
        cwd=integration_root,
    )
    assert first_run.exit_code == 0, first_run.stderr

    continuation = extract_engine_continuation("codex", first_run)
    assert continuation is not None, "codex must produce a thread_id"
    assert continuation.resume_id is not None, f"resume_id is None: {continuation}"
    thread_id = continuation.resume_id

    # Step 2: resume via exec with session context (simulates nudge)
    resume_prompt = (
        f"[Resuming prior session {thread_id}]\n\n"
        "Reply with exactly this and nothing else. Do not call tools.\n"
        "SUMMARY: resumed session\n"
        "STAGE_RESULT:\n"
        + json.dumps({
            "verdict": "pass",
            "summary": "resumed session",
            "tests": {"added": 0, "passing": 0},
            "warnings": [],
            "acceptance_criteria": [],
        })
    )
    _, resumed_run = execute_engine_prompt(
        "codex",
        prompt=resume_prompt,
        cwd=integration_root,
    )
    assert resumed_run.exit_code == 0, resumed_run.stderr
    transcript = engine.render_transcript(resumed_run)
    assert "resumed session" in transcript
