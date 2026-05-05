import pytest

from litehive.domain.runtime import SubagentRef
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.paths import task_dir
from litehive.state.persist import load_state
from litehive.state.records import save_task_runtime
from litehive.tasks.audit import load_task_audit_entries
from litehive.workspace import Workspace

from tests_integration.support.helpers import cli_command


pytestmark = pytest.mark.integration


def test_switch_cli_persists_engine_switch_and_requeues_task(integration_root) -> None:
    first = create_task(integration_root, title="Keep first queued", auto_commit=False)
    interrupted = create_task(integration_root, title="Switch interrupted task", auto_commit=False)
    interrupted.status = "interrupted"
    interrupted.pipeline_status = "implementing"
    interrupted.runtime.pipeline.execution_status = "interrupted"
    interrupted.runtime.pipeline.last_outcome.kind = "interrupted"
    interrupted.runtime.pipeline.last_outcome.stage = "implementing"
    interrupted.runtime.pipeline.last_outcome.reason_code = "execution_interrupted"
    interrupted.subagents = [
        SubagentRef(
            id="SA-0002",
            role="swe",
            engine="codex",
            status="interrupted",
            path="subagents/SA-0002-swe",
        )
    ]
    save_task(integration_root, interrupted)
    save_task_runtime(integration_root, interrupted)
    (task_dir(integration_root, interrupted) / "subagents" / "SA-0002-swe").mkdir(parents=True)

    completed = cli_command(
        "queue",
        "switch",
        interrupted.id,
        "gemini",
        "--reason",
        "Need larger context window",
        "--workspace",
        str(integration_root),
        cwd=integration_root,
    )

    assert completed.returncode == 0, completed.stderr
    assert "status: queued" in completed.stdout
    assert "pipeline_status: implementing" in completed.stdout
    assert "engine: codex -> gemini" in completed.stdout
    assert "was_active: no" in completed.stdout

    refreshed = get_task(integration_root, interrupted.id)
    assert refreshed is not None
    assert refreshed.runtime.execution.last_engine_switch is not None
    assert refreshed.runtime.execution.last_engine_switch.to_engine == "gemini"
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.execution.last_engine_switch is not None
    assert refreshed.runtime.execution.last_engine_switch.from_engine == "codex"
    assert refreshed.runtime.execution.last_engine_switch.to_engine == "gemini"
    assert refreshed.runtime.execution.last_engine_switch.reason == "Need larger context window"
    entries = load_task_audit_entries(Workspace.from_path(integration_root), task_id=interrupted.id, action="engine_switched", limit=5)
    assert entries[0].context["prior_work_paths"] == ["subagents/SA-0002-swe"]
    assert load_state(integration_root).queue == [interrupted.id, first.id]
