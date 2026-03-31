"""Subagent execution and folder persistence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from litehive.external_cli import CLIExecutionResult, parse_stage_report_text
from litehive.engines import EngineError, get_engine
from litehive.models import StageReport, SubagentRef, TaskRecord, utcnow
from litehive.tasks import save_task, task_dir


@dataclass(slots=True)
class SubagentResult:
    ref: SubagentRef
    execution: CLIExecutionResult | None
    transcript: str
    exit_code: int


class SubagentManager:
    """Run external CLI subagents inside a task-scoped folder."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def run(
        self,
        task: TaskRecord,
        *,
        role: str,
        engine_name: str,
        prompt: str,
        model: str | None = None,
    ) -> SubagentResult:
        subagent_id = f"SA-{len(task.subagents) + 1:04d}"
        folder_name = f"{subagent_id}-{role}"
        base = task_dir(self.root, task) / "subagents" / folder_name
        base.mkdir(parents=True, exist_ok=False)

        ref = SubagentRef(
            id=subagent_id,
            role=role,
            engine=engine_name,
            status="running",
            path=f"subagents/{folder_name}",
        )
        task.subagents.append(ref)
        save_task(self.root, task)

        engine = get_engine(engine_name)
        try:
            if not engine.is_available():
                raise EngineError(f"Engine '{engine.name}' is unavailable: missing binary '{engine.binary}'")
            proc = engine.run(prompt, cwd=self.root, model=model)
            transcript = proc.transcript
            ref.status = "completed" if proc.exit_code == 0 else "failed"
        except EngineError as exc:
            transcript = str(exc)
            proc = None
            ref.status = "blocked"

        save_task(self.root, task)
        self._write_session(base, ref, prompt, transcript, 0 if proc is None else proc.exit_code)
        return SubagentResult(ref=ref, execution=proc, transcript=transcript, exit_code=0 if proc is None else proc.exit_code)

    def _write_session(
        self,
        base: Path,
        ref: SubagentRef,
        prompt: str,
        transcript: str,
        exit_code: int,
    ) -> None:
        (base / "session.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": ref.id,
                    "role": ref.role,
                    "engine": ref.engine,
                    "status": ref.status,
                    "created_at": utcnow(),
                    "exit_code": exit_code,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (base / "prompt.txt").write_text(prompt, encoding="utf-8")
        (base / "transcript.md").write_text(transcript + "\n", encoding="utf-8")
        (base / "report.yaml").write_text(
            yaml.safe_dump(
                {
                    "status": ref.status,
                    "summary": transcript.splitlines()[0] if transcript else "",
                    "files_changed": [],
                    "tests": {"added": 0, "passing": 0},
                    "warnings": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )


def stage_prompt(task: TaskRecord, step: str, workspace_context: str = "") -> str:
    """Build the prompt for a stage subagent."""
    stage_instructions = {
        "grooming": (
            "Clarify the task, inspect the repo if needed, and produce a concrete execution plan. "
            "Do not make code changes in this stage."
        ),
        "implementing": (
            "Implement the task in this repository. Keep changes tightly scoped and complete the work "
            "needed for the acceptance criteria."
        ),
        "testing": (
            "Validate the implementation. Run focused checks or tests where possible and report failures "
            "precisely. Only make minimal fixes if absolutely necessary."
        ),
        "accepting": (
            "Review the current task result against the acceptance criteria and decide whether it should "
            "be accepted or sent back."
        ),
    }
    lines = [
        f"Task: {task.id} {task.title}",
        f"Stage: {step}",
        "",
        "Workspace context:",
        workspace_context.strip() or "No workspace context provided.",
        "",
        "Stage instructions:",
        stage_instructions.get(step, "Complete the requested stage."),
        "",
        "Goal:",
        task.goal or task.title,
        "",
        "Acceptance criteria:",
    ]
    if task.acceptance_criteria:
        lines.extend(f"- {item}" for item in task.acceptance_criteria)
    else:
        lines.append("- No acceptance criteria defined.")

    lines.extend(["", "Constraints:"])
    if task.constraints:
        lines.extend(f"- {item}" for item in task.constraints)
    else:
        lines.append("- Keep changes scoped to the task.")

    lines.extend(
        [
            "",
            "Return exactly this structure:",
            "VERDICT: PASS|FAIL|REJECT|BLOCKED",
            "SUMMARY: one-line summary",
            "FILES_CHANGED:",
            "- path/to/file",
            "TESTS_ADDED: <integer>",
            "TESTS_PASSING: <integer>",
            "WARNINGS:",
            "- optional warning",
        ]
    )
    return "\n".join(lines)


def stage_report_from_subagent(task: TaskRecord, step: str, result: SubagentResult) -> StageReport:
    if result.execution is not None:
        engine = get_engine(result.ref.engine)
        return engine.parse_stage_report(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            execution=result.execution,
            subagent_status=result.ref.status,
        )
    return parse_stage_report_text(
        task_id=task.id,
        step=step,  # type: ignore[arg-type]
        transcript=result.transcript,
        subagent_status=result.ref.status,
    )
