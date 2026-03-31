"""Subagent execution and folder persistence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from litehive.config import resolve_process_profile
from litehive.external_cli import CLIExecutionResult, ExternalCLIAdapter, parse_stage_report_text
from litehive.engines import EngineError, classify_execution_limit, get_engine
from litehive.models import StageReport, SubagentRef, TaskRecord, utcnow
from litehive.tasks import (
    mark_subagent_finished,
    mark_subagent_started,
    missing_acceptance_criteria_reason,
    save_task,
    task_dir,
    task_template,
)


@dataclass(slots=True)
class EngineFailure:
    kind: str
    reason: str


@dataclass(slots=True)
class SubagentResult:
    ref: SubagentRef
    execution: CLIExecutionResult | None
    transcript: str
    exit_code: int
    failure: EngineFailure | None = None


def _supports_custom_live_execution(engine: object) -> bool:
    run_live = getattr(type(engine), "run_live", None)
    return callable(run_live) and run_live is not ExternalCLIAdapter.run_live


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
        max_turns: int | None = None,
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
        mark_subagent_started(self.root, task, ref)
        self._write_session_start(base, ref, prompt)

        engine = get_engine(engine_name)
        failure: EngineFailure | None = None
        try:
            if not engine.is_available():
                raise EngineError(f"Engine '{engine.name}' is unavailable: missing binary '{engine.binary}'")
            if _supports_custom_live_execution(engine):
                if max_turns is None:
                    proc = engine.run_live(
                        prompt,
                        cwd=self.root,
                        model=model,
                        on_update=lambda execution: self._write_session_progress(
                            task,
                            base,
                            ref,
                            prompt,
                            execution,
                        ),
                    )
                else:
                    proc = engine.run_live(
                        prompt,
                        cwd=self.root,
                        model=model,
                        max_turns=max_turns,
                        on_update=lambda execution: self._write_session_progress(
                            task,
                            base,
                            ref,
                            prompt,
                            execution,
                        ),
                    )
            elif max_turns is None:
                proc = engine.run(prompt, cwd=self.root, model=model)
            else:
                proc = engine.run(prompt, cwd=self.root, model=model, max_turns=max_turns)
            transcript = engine.render_transcript(proc)
            ref.status = "completed" if proc.exit_code == 0 else "failed"
            if proc.exit_code != 0:
                limit_reason = classify_execution_limit(transcript)
                if limit_reason is not None:
                    failure = EngineFailure(kind="execution_limit", reason=limit_reason)
        except EngineError as exc:
            transcript = str(exc)
            proc = None
            ref.status = "blocked"
            failure = EngineFailure(kind="engine_error", reason=str(exc))

        save_task(self.root, task)
        mark_subagent_finished(self.root, task, ref, transcript, 0 if proc is None else proc.exit_code)
        self._write_session_finish(
            task,
            base,
            ref,
            prompt,
            transcript,
            0 if proc is None else proc.exit_code,
            proc,
        )
        return SubagentResult(
            ref=ref,
            execution=proc,
            transcript=transcript,
            exit_code=0 if proc is None else proc.exit_code,
            failure=failure,
        )

    def _write_session_start(
        self,
        base: Path,
        ref: SubagentRef,
        prompt: str,
    ) -> None:
        self._write_session_metadata(base, ref, exit_code=None)
        (base / "prompt.txt").write_text(prompt, encoding="utf-8")
        (base / "transcript.md").write_text("", encoding="utf-8")
        (base / "stdout.txt").write_text("", encoding="utf-8")
        (base / "stderr.txt").write_text("", encoding="utf-8")
        (base / "report.yaml").write_text(
            yaml.safe_dump(
                {
                    "status": ref.status,
                    "summary": "",
                    "files_changed": [],
                    "tests": {"added": 0, "passing": 0},
                    "warnings": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def _write_session_finish(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        prompt: str,
        transcript: str,
        exit_code: int,
        execution: CLIExecutionResult | None,
    ) -> None:
        report_step = (
            task.pipeline_status
            if task.pipeline_status in {"grooming", "implementing", "testing", "accepting", "commit_to_git"}
            else "implementing"
        )
        report = parse_stage_report_text(
            task_id=task.id,
            step=report_step,  # type: ignore[arg-type]
            transcript=transcript,
            subagent_status=ref.status,
        )
        self._write_session_metadata(base, ref, exit_code=exit_code)
        (base / "prompt.txt").write_text(prompt, encoding="utf-8")
        (base / "transcript.md").write_text(transcript + "\n", encoding="utf-8")
        (base / "stdout.txt").write_text("" if execution is None else execution.stdout, encoding="utf-8")
        (base / "stderr.txt").write_text("" if execution is None else execution.stderr, encoding="utf-8")
        (base / "report.yaml").write_text(
            yaml.safe_dump(
                {
                    "status": ref.status,
                    "summary": report.summary,
                    "files_changed": report.files_changed,
                    "tests": report.tests,
                    "warnings": report.warnings,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def _write_session_progress(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        prompt: str,
        execution: CLIExecutionResult,
    ) -> None:
        transcript = get_engine(ref.engine).render_transcript(execution)
        self._write_session_metadata(base, ref, exit_code=None)
        (base / "prompt.txt").write_text(prompt, encoding="utf-8")
        (base / "transcript.md").write_text(transcript, encoding="utf-8")
        (base / "stdout.txt").write_text(execution.stdout, encoding="utf-8")
        (base / "stderr.txt").write_text(execution.stderr, encoding="utf-8")
        report_step = (
            task.pipeline_status
            if task.pipeline_status in {"grooming", "implementing", "testing", "accepting", "commit_to_git"}
            else "implementing"
        )
        report_payload = {
            "status": ref.status,
            "summary": "",
            "files_changed": [],
            "tests": {"added": 0, "passing": 0},
            "warnings": [],
        }
        if transcript.strip():
            report = parse_stage_report_text(
                task_id=task.id,
                step=report_step,  # type: ignore[arg-type]
                transcript=transcript,
                subagent_status=ref.status,
            )
            report_payload = {
                "status": ref.status,
                "summary": report.summary,
                "files_changed": report.files_changed,
                "tests": report.tests,
                "warnings": report.warnings,
            }
        (base / "report.yaml").write_text(
            yaml.safe_dump(report_payload, sort_keys=False),
            encoding="utf-8",
        )

    def _write_session_metadata(
        self,
        base: Path,
        ref: SubagentRef,
        *,
        exit_code: int | None,
    ) -> None:
        created_at = utcnow()
        session_path = base / "session.yaml"
        if session_path.exists():
            existing = yaml.safe_load(session_path.read_text(encoding="utf-8")) or {}
            if isinstance(existing, dict) and isinstance(existing.get("created_at"), str):
                created_at = existing["created_at"]
        (base / "session.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": ref.id,
                    "role": ref.role,
                    "engine": ref.engine,
                    "status": ref.status,
                    "created_at": created_at,
                    "updated_at": utcnow(),
                    "exit_code": exit_code,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )


def stage_prompt(
    task: TaskRecord,
    step: str,
    workspace_context: str = "",
    *,
    process_profile: str = "generic",
) -> str:
    """Build the prompt for a stage subagent."""
    stage_instructions = {
        "grooming": [
            "Clarify the task, inspect the repo if needed, and produce a concrete execution plan.",
            "Do not make code changes in this stage.",
        ],
        "implementing": [
            "Implement the task in this repository.",
            "Keep changes tightly scoped and complete the work needed for the acceptance criteria.",
        ],
        "testing": [
            "Validate the implementation.",
            "Run focused checks or tests where possible and report failures precisely.",
            "Only make minimal fixes if absolutely necessary.",
        ],
        "accepting": [
            "Review the current task result against the acceptance criteria and decide whether it should be accepted or sent back.",
        ],
    }
    profile = resolve_process_profile(process_profile)
    workspace_overlay = profile.get("workspace_overlay", [])
    stage_overlay = profile.get("stage_overlay", {}).get(step, [])
    lines = [
        f"Task: {task.id} {task.title}",
        f"Stage: {step}",
        f"Process profile: {profile['label']}",
        f"Task type: {task.task_type or '-'}",
        "",
        "Workspace context:",
        workspace_context.strip() or "No workspace context provided.",
        "",
        "Shared process:",
        f"- Orchestrator model: {profile['orchestrator_model']}",
        f"- Routing model: {profile['routing_model']}",
        f"- Shared stages: {' -> '.join(profile['shared_stages'])}.",
        f"- Role model: {profile['role_model']}",
        f"- Source of truth: {profile['source_of_truth']}",
        f"- Task source of truth: {profile['task_source_of_truth']}",
        f"- TDD expectations: {profile['tdd_expectations']}",
        f"- Verification discipline: {profile['verification_discipline']}",
        f"- Acceptance flow: {profile['acceptance_flow']}",
        f"- Commit and recovery: {profile['commit_recovery']}",
        "",
        "Project overlay:",
        f"- {profile['summary']}",
    ]
    lines.extend(workspace_overlay or ["- No project-specific overlay provided."])
    lines.extend(
        [
            "",
            "Prompt scaffold:",
            *profile.get("prompt_scaffold", []),
            "",
            "Stage instructions:",
            *stage_instructions.get(step, ["Complete the requested stage."]),
        ]
    )
    lines.extend(stage_overlay)
    lines.extend(
        [
            "",
            "Goal:",
            task.goal or task.title,
            "",
            "Acceptance criteria:",
        ]
    )
    if task.acceptance_criteria:
        lines.extend(f"- {item}" for item in task.acceptance_criteria)
    else:
        lines.append("- No acceptance criteria defined.")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        lines.extend(
            [
                "",
                "Acceptance gate:",
                f"- {missing_criteria_reason}",
                "- Use grooming or task intake to define the missing criteria before implementation starts.",
            ]
        )

    template = task_template(task)
    if template is not None:
        lines.extend(
            [
                "",
                "Task template:",
                f"- Use the `{task.task_type}` template to keep the task structured.",
            ]
        )
        prompt_guidance = template.get("prompt_guidance", [])
        if isinstance(prompt_guidance, list):
            lines.extend(f"- {item}" for item in prompt_guidance)
        brief_sections = template.get("brief_sections", [])
        if isinstance(brief_sections, list):
            lines.extend(["", "Template sections to fill or verify:"])
            lines.extend(f"- {item}" for item in brief_sections)

    lines.extend(["", "Plan:"])
    if task.plan:
        lines.extend(f"- {item}" for item in task.plan)
    else:
        lines.append("- No plan defined.")

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
