from pathlib import Path

from heru.types import SubagentRef

from litehive.agents.execution_trace import load_subagent_execution_trace
from litehive.agents.session_store import save_subagent_artifacts
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task
from litehive.tasks.paths import task_dir
from litehive.workspace import Workspace


def test_load_subagent_execution_trace_prefers_session_event_stream_over_cached_trace(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task(tmp_path, title="Trace source priority")
    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="completed",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    base = task_dir(tmp_path, task) / ref.path
    base.mkdir(parents=True)
    (base / "execution_trace.md").write_text("cached file trace", encoding="utf-8")
    save_subagent_artifacts(
        workspace,
        task.id,
        ref.id,
        event_stream={
            "events": [
                {
                    "kind": "message",
                    "engine": "codex",
                    "content": "sqlite event trace",
                }
            ]
        },
    )

    trace = load_subagent_execution_trace(workspace, task, ref, active=False)

    assert trace is not None
    assert trace.text == "sqlite event trace"
    assert trace.source == "subagent_sessions:event_stream"
    assert not trace.cached_final_snapshot


def test_load_subagent_execution_trace_keeps_cached_trace_as_legacy_fallback(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task(tmp_path, title="Trace file fallback")
    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="completed",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    base = task_dir(tmp_path, task) / ref.path
    base.mkdir(parents=True)
    (base / "execution_trace.md").write_text("cached file trace", encoding="utf-8")

    trace = load_subagent_execution_trace(workspace, task, ref, active=False)

    assert trace is not None
    assert trace.text == "cached file trace"
    assert trace.source == base / "execution_trace.md"
    assert trace.cached_final_snapshot
