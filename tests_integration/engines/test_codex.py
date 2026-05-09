import pytest

from litehive.agents.session_store import SubagentArtifactPayload, subagent_artifacts
from litehive.state.records import WorkspaceTasks
from litehive.tasks.queue import TaskQueueService
from litehive.tasks.activity import task_activity_store_for_task
from litehive.workspace import Workspace

from tests_integration.support.helpers import (
    assert_nudge_verdict_submission,
    assert_successful_smoke_session,
    execute_engine_prompt,
    litehive_python_shell_prefix,
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
    workspace = Workspace.from_path(integration_root)
    task = WorkspaceTasks(workspace).create( title="Integration report task", auto_commit=False)
    TaskQueueService(workspace).mark_active(task.id)
    subagent_id = "SI-codex-report"
    subagent_artifacts(workspace, task.id, subagent_id).save(
        session=SubagentArtifactPayload({"id": subagent_id, "role": "swe", "engine": "codex", "status": "running"}),
    )
    prompt = (
        f"Run `{litehive_python_shell_prefix()}LITEHIVE_AGENT_ROLE=swe LITEHIVE_SUBAGENT_ID={subagent_id} "
        f'"$LITEHIVE_PYTHON_PATH" -m litehive.main agent report --verdict pass --stage implementing --task-id {task.id} '
        '--workspace . --message "integration report from codex"` exactly once.'
    )
    _, execution = execute_engine_prompt(
        "codex",
        prompt=prompt,
        cwd=integration_root,
        extra_env={
            "LITEHIVE_AGENT_ROLE": "swe",
            "LITEHIVE_SUBAGENT_ID": subagent_id,
            "LITEHIVE_STAGE": "implementing",
        },
    )
    assert execution.exit_code == 0, execution.transcript
    thread = task_activity_store_for_task(workspace, WorkspaceTasks(workspace).require(task.id)).load()
    assert thread[-1].role == "swe"
    assert thread[-1].stage == "implementing"
    assert thread[-1].verdict == "pass"
    assert thread[-1].message == "integration report from codex"
    assert thread[-1].source_subagent_id == subagent_id


def test_codex_nudge_submits_verdict_via_cli(codex_smoke_session) -> None:
    assert_nudge_verdict_submission("codex", smoke_session=codex_smoke_session)
