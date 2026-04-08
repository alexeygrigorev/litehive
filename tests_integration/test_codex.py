import pytest

from litehive.tasks import create_task, load_task_thread, require_task, set_active_task

from .helpers import (
    assert_nudge_verdict_submission,
    assert_successful_smoke_session,
    execute_engine_prompt,
    prepare_smoke_session,
    require_real_engine,
)


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def codex_smoke_session(module_integration_root):
    return prepare_smoke_session("codex", cwd=module_integration_root)


def test_codex_smoke_prompt_succeeds(codex_smoke_session) -> None:
    assert_successful_smoke_session(codex_smoke_session)


def test_codex_can_invoke_litehive_report_and_persist_thread_comment(integration_root) -> None:
    require_real_engine("codex")
    task = create_task(integration_root, title="Integration report task", auto_commit=False)
    set_active_task(integration_root, task.id)
    prompt = (
        f'Run `litehive report --verdict pass --role swe --step implementing --task-id {task.id} '
        '--workspace . --message "integration report from codex"` exactly once.'
    )
    _, execution = execute_engine_prompt("codex", prompt=prompt, cwd=integration_root)
    assert execution.exit_code == 0, execution.transcript
    thread = load_task_thread(integration_root, require_task(integration_root, task.id))
    assert thread[-1].role == "swe"
    assert thread[-1].step == "implementing"
    assert thread[-1].verdict == "pass"
    assert thread[-1].message == "integration report from codex"


def test_codex_nudge_submits_verdict_via_cli(codex_smoke_session) -> None:
    assert_nudge_verdict_submission("codex", smoke_session=codex_smoke_session)
