from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from heru.base import CLIExecutionResult
from heru.types import SubagentRef

from litehive.container import build_subagent_manager
from litehive.agents.report_extraction import AgentReportService
from litehive.config.workspace import create_workspace
from litehive.domain.agent import EngineFailure, ExecutionTrace, SubagentResult
from litehive.domain.common import TaskStage
from litehive.feedback import FEEDBACK_CAP
from litehive.domain.reports import SEMANTIC_REJECT_CLASSIFICATION, StageReport, TaskActivityEntry
from litehive.state.records import WorkspaceTasks
from litehive.tasks.paths import read_text_artifact, resolve_artifact_path
from litehive.tasks.activity_rendering import append_activity_entry
from litehive.workspace import Workspace


def _build_manager(workspace: Workspace, *, execution_root: Path) -> Any:
    return build_subagent_manager(
        workspace,
        workspace.load_config(),
        execution_root=execution_root,
    )


def _stub_execution(exit_code: int = 0) -> CLIExecutionResult:
    return CLIExecutionResult(
        adapter="test",
        argv=("test",),
        cwd=Path("/tmp"),
        exit_code=exit_code,
        stdout="",
        stderr="",
        pid=0,
    )


def _subagent_result(
    *,
    execution_trace: str,
    failure: EngineFailure | None = None,
) -> SubagentResult:
    return SubagentResult(
        ref=SubagentRef(
            id="SA-0001",
            role="swe",
            engine="codex",
            status="failed" if failure is not None else "completed",
            path="subagents/SA-0001-swe",
        ),
        execution=_stub_execution(exit_code=1 if failure is not None else 0),
        execution_trace=ExecutionTrace.from_text(execution_trace),
        exit_code=1 if failure is not None else 0,
        failure=failure,
    )


def test_agent_report_service_preserves_cli_message_verbatim(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create(title="Keep CLI summary untouched")
    message = "summary line\n\n" + ("y" * (FEEDBACK_CAP + 250))

    append_activity_entry(
        workspace,
        task,
        TaskActivityEntry(
            role="swe",
            stage="implementing",
            verdict="pass",
            message=message,
            source_subagent_id="SA-0001",
        ),
    )

    report = AgentReportService(workspace).stage_report_from_subagent(
        task,
        TaskStage.IMPLEMENTING,
        _subagent_result(execution_trace="x" * (FEEDBACK_CAP + 500)),
    )

    assert report.submitted_via_cli is True
    assert report.summary == "summary line"
    assert report.feedback == message


def test_agent_report_service_preserves_semantic_reject_classification(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create(title="Classified reviewer reject")

    append_activity_entry(
        workspace,
        task,
        TaskActivityEntry(
            role="reviewer",
            stage="accepting",
            verdict="reject",
            verdict_classification=SEMANTIC_REJECT_CLASSIFICATION,
            message="acceptance evidence is incomplete",
            source_subagent_id="SA-0001",
        ),
    )

    report = AgentReportService(workspace).stage_report_from_subagent(
        task,
        TaskStage.ACCEPTING,
        _subagent_result(execution_trace="reviewer submitted reject"),
    )

    assert report.submitted_via_cli is True
    assert report.verdict == "reject"
    assert report.failure_classification == SEMANTIC_REJECT_CLASSIFICATION
    assert report.failure_diagnostics["verdict_classification"] == SEMANTIC_REJECT_CLASSIFICATION


def test_stage_report_uses_pipeline_state_without_files_changed() -> None:
    report = StageReport(
        task_id="T-0001",
        pipeline_state="implementing",
        verdict="pass",
        summary="implemented the change",
    )

    payload = report.model_dump(mode="json")

    assert payload["pipeline_state"] == "implementing"
    assert "stage" not in payload
    assert "files_changed" not in payload


def test_stage_report_rejects_comment_verdicts_and_legacy_files_changed() -> None:
    # Use model_validate with dict payloads so the test exercises the
    # validation API boundary (which accepts arbitrary mappings) instead of
    # static-typing the bad inputs out at the keyword-argument layer.
    with pytest.raises(ValidationError, match="verdict"):
        StageReport.model_validate(
            {
                "task_id": "T-0001",
                "pipeline_state": "implementing",
                "verdict": "comment",
                "summary": "operator note",
            }
        )

    with pytest.raises(ValidationError, match="files_changed"):
        StageReport.model_validate(
            {
                "task_id": "T-0001",
                "pipeline_state": "implementing",
                "verdict": "pass",
                "summary": "implemented the change",
                "files_changed": ["src/app.py"],
            }
        )


@pytest.mark.parametrize("verdict", ["accept", "fail"])
def test_task_activity_entry_rejects_unsupported_submitted_verdicts(verdict: str) -> None:
    with pytest.raises(ValidationError, match="verdict"):
        TaskActivityEntry.model_validate(
            {
                "role": "swe",
                "stage": "implementing",
                "verdict": verdict,
                "message": "legacy failure wording",
            }
        )


def test_subagent_manager_keeps_full_transcript_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create(title="Keep full transcript artifacts")
    manager = _build_manager(workspace, execution_root=tmp_path)
    transcript = "full transcript\n" + ("z" * (FEEDBACK_CAP + 400))

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            extra_env: dict[str, str] | None = None,
        ) -> CLIExecutionResult:
            del prompt, model, extra_env
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout=transcript,
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            del execution
            return transcript

    monkeypatch.setattr("litehive.agents.engine_manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")
    subagent_dir = workspace.task_dir(task) / result.ref.path

    transcript_cache = resolve_artifact_path(subagent_dir, "execution_trace.md")
    assert transcript_cache is not None
    assert read_text_artifact(transcript_cache) == f"{transcript}\n"
    assert (subagent_dir / "stdout.txt").read_text(encoding="utf-8") == transcript
