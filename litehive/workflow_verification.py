"""Workflow lifecycle verification scope and evidence heuristics."""

from __future__ import annotations

from pathlib import Path
import re

import yaml

from litehive.models import StageReport, TaskRecord
from litehive.tasks import task_dir


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def workflow_verification_scope(task: TaskRecord) -> dict[str, bool]:
    haystack = "\n".join(
        [
            task.title,
            task.goal,
            *task.acceptance_criteria,
            *task.plan,
            *task.constraints,
        ]
    ).lower()
    normalized = re.sub(r"[_\-/]+", " ", haystack)
    workflow = any(
        token in normalized
        for token in (
            "workflow",
            "control-plane",
            "control plane",
            "lifecycle",
            "runner",
            "routing",
            "queue",
            "pool",
            "run-all",
            "run all",
            "drain",
            "wrapper",
            "commit_to_git",
            "commit to git",
        )
    )
    commit = any(
        token in normalized
        for token in (
            "commit_to_git",
            "commit to git",
            "checkpoint commit",
            "final checkpoint commit",
            "completion reliability",
            "commit reliability",
            "checkpoint",
            "clean completion",
            "final task commit",
        )
    )
    resume = any(
        token in normalized
        for token in (
            "resume",
            "resum",
            "resumable",
            "recovery",
            "recover",
            "interrupted",
        )
    )
    run_all = any(
        token in normalized
        for token in (
            "run-all",
            "run all",
            "pool",
            "drain",
            "wrapper",
        )
    )
    return {
        "workflow": workflow,
        "commit": commit,
        "resume": resume,
        "run_all": run_all,
    }


def workflow_verification_gaps(task: TaskRecord, evidence_text: str) -> list[str]:
    scope = workflow_verification_scope(task)
    if not scope["workflow"]:
        return []

    text = evidence_text.lower()
    normalized = re.sub(r"[_\-/]+", " ", text)

    invocation_signals = (
        "uv run litehive",
        " litehive run",
        " litehive resume",
        " litehive status",
        " litehive queue",
        " litehive repair",
        "scripts/run-all.sh",
        "run-all.sh",
        "--drain",
        "bash scripts/run-all.sh",
    )
    observed_result_signals = (
        "stage_outcomes:",
        "stage_outcomes=",
        "status:",
        "pipeline_status:",
        "pipeline_status=",
        "commit:",
        "resumable_tasks:",
        "resumable:",
        "stop_reason:",
        "== iteration ",
        "no active or queued tasks remain. stopping.",
        "pool already stopped:",
    )
    real_lifecycle = (
        _contains_any(text, invocation_signals)
        or _contains_any(text, ("== iteration ", "no active or queued tasks remain. stopping.", "pool already stopped:"))
    ) and _contains_any(text, observed_result_signals)

    gaps: list[str] = []
    if not real_lifecycle:
        gaps.append(
            "workflow/control-plane tasks require real Litehive CLI or wrapper lifecycle evidence, not helper-only or unit-test-only verification"
        )

    if scope["commit"]:
        commit_terms = (
            "commit_to_git succeeded",
            "commit to git succeeded",
            "final checkpoint commit",
            "checkpoint commit",
            "commit to git=pass",
            "git commit",
            "commit_sha",
            "commit sha",
            "git rev-parse",
            "git log",
            "commit: ",
        )
        if not _contains_any(normalized, commit_terms):
            gaps.append(
                "completion or commit reliability claims require proof that commit_to_git succeeded and recorded the final checkpoint commit"
            )
        clean_completion_terms = (
            "status: done",
            "pipeline_status: done",
            "pipeline_status=done",
            "execution_status: done",
            "completed:",
            "commit_to_git=pass",
            "commit: ",
        )
        if not _contains_any(normalized, clean_completion_terms):
            gaps.append(
                "completion reliability claims also require proof that Litehive recorded clean completion, not only that git produced a commit"
            )

    if scope["resume"]:
        resume_terms = (
            "resume from",
            "resumed from",
            "pipeline_status",
            "correct stage",
            "interrupted",
            "resume",
        )
        if not ("resume" in normalized and any(term in normalized for term in resume_terms)):
            gaps.append(
                "resume or recovery claims require proof that an interrupted task resumed from the correct stage in a real workspace run"
            )

    if scope["run_all"]:
        run_all_terms = (
            "run-all.sh",
            "--drain",
            "== iteration ",
            "no active or queued tasks remain. stopping.",
            "pool already stopped:",
            "0001-run.log",
        )
        if not _contains_any(normalized, run_all_terms):
            gaps.append(
                "run-all, drain, wrapper, or pool claims require proof through the wrapper or real CLI execution"
            )

    return gaps


def latest_stage_report(root: Path, task: TaskRecord, step: str) -> StageReport | None:
    reports_dir = task_dir(root, task) / "reports"
    candidates = sorted(reports_dir.glob(f"{step}-*.yaml"))
    if not candidates:
        return None
    payload = yaml.safe_load(candidates[-1].read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    return StageReport.model_validate(payload)
