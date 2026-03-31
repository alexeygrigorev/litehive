"""Deterministic local task runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from litehive.models import StageReport, TaskRecord
from litehive.tasks import mark_stage_finished, mark_stage_started, save_task, task_dir


class StageExecutor(Protocol):
    def __call__(self, task: TaskRecord, step: str) -> StageReport: ...


@dataclass(slots=True)
class RunResult:
    final_status: str
    steps_executed: int = 0
    last_verdict: str | None = None


_STEPS_FROM: dict[str, str] = {
    "backlog": "grooming",
    "grooming": "grooming",
    "implementing": "implementing",
    "testing": "testing",
    "accepting": "accepting",
    "commit_to_git": "commit_to_git",
}

_ROUTES: dict[tuple[str, str], str] = {
    ("grooming", "pass"): "implementing",
    ("grooming", "accept"): "implementing",
    ("implementing", "pass"): "testing",
    ("implementing", "accept"): "testing",
    ("testing", "pass"): "accepting",
    ("testing", "accept"): "accepting",
    ("testing", "fail"): "implementing",
    ("testing", "reject"): "implementing",
    ("accepting", "pass"): "commit_to_git",
    ("accepting", "accept"): "commit_to_git",
    ("accepting", "fail"): "implementing",
    ("accepting", "reject"): "implementing",
    ("commit_to_git", "pass"): "done",
    ("commit_to_git", "accept"): "done",
    ("commit_to_git", "fail"): "flagged",
    ("commit_to_git", "reject"): "flagged",
    ("commit_to_git", "blocked"): "flagged",
}


class TaskExecutionRunner:
    """Drive one task through the fixed pipeline using a deterministic router."""

    def __init__(self, root: Path, executor: StageExecutor, max_rejections: int = 3) -> None:
        self.root = root
        self.executor = executor
        self.max_rejections = max_rejections

    def run(self, task: TaskRecord) -> RunResult:
        steps = 0
        rejections = 0
        last_verdict: str | None = None

        if task.pipeline_status == "done":
            return RunResult(final_status="done")

        while True:
            current = _STEPS_FROM.get(task.pipeline_status)
            if current is None:
                return RunResult(final_status=task.status, steps_executed=steps, last_verdict=last_verdict)

            task.status = "in_progress"
            task.pipeline_status = current  # type: ignore[assignment]
            save_task(self.root, task)
            mark_stage_started(self.root, task, current)

            report = self.executor(task, current)
            self._write_report(task, report, steps + 1)
            mark_stage_finished(self.root, task, report)

            steps += 1
            last_verdict = report.verdict
            target = _ROUTES.get((current, report.verdict))
            if target is None:
                task.status = "flagged"
                save_task(self.root, task)
                return RunResult("flagged", steps, last_verdict)

            if target == "implementing" and current in {"testing", "accepting"}:
                rejections += 1
                if rejections >= self.max_rejections:
                    task.status = "flagged"
                    save_task(self.root, task)
                    return RunResult("flagged", steps, last_verdict)

            if target == "done":
                if current != "commit_to_git":
                    task.pipeline_status = "done"
                    task.status = "done"
                    save_task(self.root, task)
                return RunResult("done", steps, last_verdict)

            if target == "flagged":
                task.status = "flagged"
                save_task(self.root, task)
                return RunResult("flagged", steps, last_verdict)

            task.pipeline_status = target  # type: ignore[assignment]
            save_task(self.root, task)

    def _write_report(self, task: TaskRecord, report: StageReport, ordinal: int) -> None:
        reports_dir = task_dir(self.root, task) / "reports"
        filename = f"{report.step}-{ordinal:03d}.yaml"
        (reports_dir / filename).write_text(
            yaml.safe_dump(report.model_dump(mode="python"), sort_keys=False),
            encoding="utf-8",
        )
