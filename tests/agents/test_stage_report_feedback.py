from pathlib import Path

import pytest
from pydantic import ValidationError

from heru.base import CLIExecutionResult
from heru.types import SubagentRef

from litehive.agents.manager import SubagentManager
from litehive.agents.parsing import stage_report_from_subagent
from litehive.config.workspace import ensure_workspace
from litehive.domain.agent import EngineFailure, SubagentResult
from litehive.domain.common import FEEDBACK_CAP, TRUNCATION_MARKER
from litehive.domain.reports import TaskActivityEntry
from litehive.domain.runtime import ResourceLimitEvent
from litehive.state.records import create_task
from litehive.tasks.paths import task_dir
from litehive.tasks.reports import append_activity_entry


def _subagent_result(
    *,
    transcript: str,
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
        execution=None,
        transcript=transcript,
        exit_code=1 if failure is not None else 0,
        failure=failure,
    )


def test_stage_report_from_subagent_caps_resource_limit_feedback(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Cap resource-limit feedback")
    transcript = "x" * (FEEDBACK_CAP + 300)

    report = stage_report_from_subagent(
        task,
        "implementing",
        _subagent_result(
            transcript=transcript,
            failure=EngineFailure(
                kind="resource_limit",
                reason="memory limit reached",
                resource_limit_event=ResourceLimitEvent(
                    resource="memory",
                    reason="memory limit reached",
                ),
            ),
        ),
    )

    assert len(report.feedback) == FEEDBACK_CAP
    assert report.feedback.endswith(TRUNCATION_MARKER)
    assert report.feedback != transcript


def test_stage_report_from_subagent_preserves_cli_message_verbatim(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Keep CLI summary untouched")
    message = "summary line\n\n" + ("y" * (FEEDBACK_CAP + 250))

    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(
            role="swe",
            stage="implementing",
            verdict="pass",
            message=message,
        ),
    )

    report = stage_report_from_subagent(
        task,
        "implementing",
        _subagent_result(transcript="x" * (FEEDBACK_CAP + 500)),
        root=tmp_path,
    )

    assert report.submitted_via_cli is True
    assert report.summary == "summary line"
    assert report.feedback == message


def test_task_activity_entry_rejects_removed_fail_verdict_alias() -> None:
    with pytest.raises(ValidationError, match="verdict"):
        TaskActivityEntry(
            role="swe",
            stage="implementing",
            verdict="fail",
            message="legacy failure wording",
        )


def test_subagent_manager_keeps_full_transcript_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Keep full transcript artifacts")
    manager = SubagentManager(tmp_path)
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

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")
    subagent_dir = task_dir(tmp_path, task) / result.ref.path

    assert (subagent_dir / "transcript.md").read_text(encoding="utf-8") == f"{transcript}\n"
    assert (subagent_dir / "stdout.txt").read_text(encoding="utf-8") == transcript
