
import pytest

from litehive.domain.runtime import RuntimeContinuationHandoff, RuntimeSubagentState
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.paths import task_dir
from litehive.tasks.persistence import load_state
from litehive.state.records import save_task_runtime

from .helpers import cli_command


pytestmark = pytest.mark.integration


def test_switch_cli_persists_engine_handoff_and_requeues_task(integration_root) -> None:
    first = create_task(integration_root, title="Keep first queued", auto_commit=False)
    interrupted = create_task(integration_root, title="Switch interrupted task", auto_commit=False)
    interrupted.status = "interrupted"
    interrupted.pipeline_status = "implementing"
    interrupted.runtime.execution_status = "interrupted"
    interrupted.runtime.last_outcome.kind = "interrupted"
    interrupted.runtime.last_outcome.stage = "implementing"
    interrupted.runtime.last_outcome.reason_code = "execution_interrupted"
    interrupted.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0002",
        role="swe",
        engine="codex",
        status="interrupted",
        path="subagents/SA-0002-swe",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:30+00:00",
        completed_at="2026-04-01T00:00:30+00:00",
        transcript_snippet="implementation half done",
    )
    interrupted.runtime.continuation_handoff = RuntimeContinuationHandoff(
        step="implementing",
        kind="restart",
        reason="Need a different engine",
        from_engine="codex",
        to_engine="codex",
        subagent_id="SA-0002",
        subagent_path="subagents/SA-0002-swe",
        status="interrupted",
        summary="implementation half done",
        transcript_snippet="implementation half done",
        warnings=[],
        transcript_path="subagents/SA-0002-swe/transcript.md",
        updated_at="2026-04-01T00:00:30+00:00",
    )
    save_task(integration_root, interrupted)
    save_task_runtime(integration_root, interrupted)
    (task_dir(integration_root, interrupted) / "subagents" / "SA-0002-swe").mkdir(parents=True)

    completed = cli_command(
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
    assert refreshed.runtime.last_engine_switch is not None
    assert refreshed.runtime.last_engine_switch.to_engine == "gemini"
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.continuation_handoff is not None
    assert refreshed.runtime.continuation_handoff.subagent_path == "subagents/SA-0002-swe"
    assert refreshed.runtime.last_engine_switch is not None
    assert refreshed.runtime.last_engine_switch.from_engine == "codex"
    assert refreshed.runtime.last_engine_switch.to_engine == "gemini"
    assert refreshed.runtime.last_engine_switch.reason == "Need larger context window"
    assert load_state(integration_root).queue == [interrupted.id, first.id]
