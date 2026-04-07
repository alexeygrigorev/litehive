"""Task storage helpers for the local YAML workspace."""

from dataclasses import dataclass, field
import fcntl
import gzip
import re
from contextlib import contextmanager
import os
import signal
import shutil
from pathlib import Path
import sys
import threading
import time
from typing import TextIO, get_args

import yaml

from litehive.config import (
    VALID_POOL_SELECTION_POLICIES,
    ensure_workspace,
    load_config,
    render_workspace_gitignore,
    state_path,
    workspace_dir,
    workspace_gitignore_path,
)


from litehive.git_ops import (
    GitError,
    checkpoint_message,
    current_head,
    default_commit_message,
    find_commit_by_subject,
    is_git_repo,
    status_porcelain,
)
from litehive.models import (
    FollowUpTaskSpec,
    PlannedEffort,
    RecoveryAction,
    RecoveryEvidenceItem,
    RecoveryReport,
    RuntimeContinuationHandoff,
    RuntimeEngineContinuation,
    RuntimeEngineSwitch,
    RuntimeInterruptionState,
    RunnerStatusState,
    ResourceLimitEvent,
    RuntimeSubagentState,
    StageReport,
    TaskCreationSource,
    SubagentRef,
    TaskOutcomeState,
    TaskComplexity,
    UpstreamContributionOrigin,
    TaskRecord,
    TaskRuntime,
    WorkspaceState,
    utcnow,
)

VALID_TASK_PRIORITIES = {"low", "medium", "high", "critical"}
VALID_TASK_ENGINES = {"codex", "opencode", "gemini", "copilot", "claude", "goz"}
VALID_HUMAN_CHECKPOINTS = {"before_acceptance", "before_commit"}
VALID_PM_COMPLEXITIES = set(get_args(TaskComplexity))
VALID_PLANNED_EFFORTS = set(get_args(PlannedEffort))
VALID_TASK_TYPES = {"adapter", "bugfix", "docs", "intake", "refactor", "research", "review"}
TASK_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(slots=True)
class _RunnerLockState:
    handle: TextIO
    depth: int
    status: RunnerStatusState
    owner_thread_id: int = 0
    metadata_lock: threading.Lock = field(default_factory=threading.Lock)


_RUNNER_LOCKS: dict[Path, _RunnerLockState] = {}
_RUNNER_LOCKS_MUTEX = threading.Lock()
_MISSING = object()
# A live runner that hasn't refreshed its heartbeat in this many seconds is considered late.
HEARTBEAT_LATE_THRESHOLD_SECONDS = 60
CLOSED_TASK_STATUSES = {"cancelled", "wont_do", "deferred", "duplicate"}
RESUMABLE_TASK_STATUSES = {"interrupted", "parked"}
TASK_TEMPLATES: dict[str, dict[str, object]] = {
    "adapter": {
        "goal": "Define the adapter change clearly and land the required integration behavior.",
        "acceptance_criteria": [
            "The adapter entrypoint, inputs, and expected outputs are defined for the target integration.",
            "Configuration, invocation, and failure handling are updated consistently at the adapter boundary.",
            "Focused verification covers the adapter path with a representative task run or test.",
        ],
        "constraints": [
            "Keep provider-specific behavior isolated to the adapter boundary.",
            "Preserve deterministic workspace state and execution flow.",
        ],
        "plan": [
            "Inspect the existing adapter interface, config wiring, and invocation flow.",
            "Implement the adapter change close to the integration seam.",
            "Verify the adapter path with a focused test or representative run.",
        ],
        "prompt_guidance": [
            "State the target adapter seam, external dependency, and expected contract up front.",
            "Call out config, invocation, and failure-path changes explicitly.",
            "Prefer verification that exercises the adapter boundary rather than unrelated paths.",
        ],
        "brief_sections": [
            "Adapter surface: identify the entrypoint, inputs, outputs, and external system involved.",
            "Config and execution path: note which settings, command wiring, or failure handling must change.",
            "Verification evidence: capture the focused run or test that proves the adapter path works.",
        ],
        "brief_section_stubs": [
            {
                "title": "Adapter Surface",
                "prompt": "Identify the entrypoint, inputs, outputs, and external system involved.",
            },
            {
                "title": "Config and Execution Path",
                "prompt": "Note which settings, command wiring, or failure handling must change.",
            },
            {
                "title": "Verification Evidence",
                "prompt": "Capture the focused run or test that proves the adapter path works.",
            },
        ],
    },
    "bugfix": {
        "goal": "Identify the failing behavior, implement the fix, and lock it down with focused verification.",
        "acceptance_criteria": [
            "The bug or regression is described clearly enough to verify before and after behavior.",
            "The fix addresses the root cause in the affected path without broad unrelated changes.",
            "A regression test or focused verification demonstrates the issue is resolved.",
        ],
        "constraints": [
            "Prefer the smallest change that removes the failure mode.",
            "Call out any remaining edge cases or follow-up risk explicitly.",
        ],
        "plan": [
            "Reproduce or localize the failing behavior.",
            "Implement the minimal targeted fix.",
            "Run focused regression coverage for the affected behavior.",
        ],
        "prompt_guidance": [
            "Describe the broken behavior, trigger, and expected correct behavior before changing code.",
            "Aim at root cause, not just the visible symptom.",
            "Include regression coverage or equivalent focused proof that the failure is gone.",
        ],
        "brief_sections": [
            "Bug and reproduction: describe the failing behavior, trigger, and expected result.",
            "Root cause: note the suspected or confirmed cause in the affected path.",
            "Regression coverage: record the exact test or check that prevents recurrence.",
        ],
        "brief_section_stubs": [
            {
                "title": "Bug and Reproduction",
                "prompt": "Describe the failing behavior, trigger, and expected result.",
            },
            {
                "title": "Root Cause",
                "prompt": "Note the suspected or confirmed cause in the affected path.",
            },
            {
                "title": "Regression Coverage",
                "prompt": "Record the exact test or check that prevents recurrence.",
            },
        ],
    },
    "research": {
        "goal": "Answer the open question with concrete evidence and a recommendation for next action.",
        "acceptance_criteria": [
            "The research question, scope, and decision to inform are stated clearly.",
            "Findings are grounded in repository evidence, experiments, or direct inspection.",
            "The output includes a recommendation, tradeoffs, and any follow-up tasks.",
        ],
        "constraints": [
            "Prefer evidence from the repository and local experiments over speculation.",
            "Keep conclusions explicit about confidence and remaining unknowns.",
        ],
        "plan": [
            "Define the exact question and scope of the investigation.",
            "Gather evidence from code, configs, tests, or focused experiments.",
            "Summarize findings, recommendation, and concrete follow-up actions.",
        ],
        "prompt_guidance": [
            "Frame the question, scope, and decision this research should inform.",
            "Separate observed evidence from inference.",
            "End with a recommendation, tradeoffs, and concrete follow-up tasks.",
        ],
        "brief_sections": [
            "Question and scope: define what is being investigated and what is out of scope.",
            "Evidence: capture repository findings, experiments, or comparisons that support the answer.",
            "Recommendation: state the proposed next action, tradeoffs, and remaining unknowns.",
        ],
        "brief_section_stubs": [
            {
                "title": "Question and Scope",
                "prompt": "Define what is being investigated and what is out of scope.",
            },
            {
                "title": "Evidence",
                "prompt": "Capture repository findings, experiments, or comparisons that support the answer.",
            },
            {
                "title": "Recommendation",
                "prompt": "State the proposed next action, tradeoffs, and remaining unknowns.",
            },
        ],
    },
    "review": {
        "goal": "Review the target change critically and produce an actionable decision with supporting evidence.",
        "acceptance_criteria": [
            "Findings are prioritized by severity and tied to concrete files or behaviors.",
            "Open questions, assumptions, and residual risks are captured explicitly.",
            "The review result makes the next action clear: accept, revise, or investigate further.",
        ],
        "constraints": [
            "Focus on correctness, regressions, and missing verification before style nits.",
            "Keep findings concrete enough that another engineer can act on them directly.",
        ],
        "plan": [
            "Inspect the relevant change or workflow surface.",
            "Identify actionable findings and supporting evidence.",
            "Summarize the decision, open questions, and required follow-up.",
        ],
        "prompt_guidance": [
            "Prioritize correctness, regressions, and missing verification over style observations.",
            "Tie each finding to a concrete file, behavior, or risk.",
            "Make the decision explicit: accept, revise, or investigate further.",
        ],
        "brief_sections": [
            "Review scope: identify the change, workflow, or files under review.",
            "Findings: record actionable issues with severity and supporting evidence.",
            "Decision: capture accept versus revise plus open questions or residual risks.",
        ],
        "brief_section_stubs": [
            {
                "title": "Review Scope",
                "prompt": "Identify the change, workflow, or files under review.",
            },
            {
                "title": "Findings",
                "prompt": "Record actionable issues with severity and supporting evidence.",
            },
            {
                "title": "Decision",
                "prompt": "Capture accept versus revise plus open questions or residual risks.",
            },
        ],
    },
    "intake": {
        "goal": "Capture a brain dump or freeform specification and prepare it for further decomposition.",
        "acceptance_criteria": [
            "The original brain dump is preserved and accessible.",
            "The rough task title and goal accurately reflect the high-level intent.",
            "The task is queued in 'tasks' mode for further grooming.",
        ],
        "constraints": [
            "Do not try to fully scope or structure the work at intake time.",
            "Preserve the original dump as the authoritative source of intent.",
        ],
        "plan": [
            "Review the brain dump for high-level intent.",
            "Extract a concise title and clear goal statement.",
            "Prepare the task for planner grooming.",
        ],
        "prompt_guidance": [
            "Keep the scope high-level; the planner will handle decomposition later.",
            "Ensure the original intent is preserved and linked to the task.",
        ],
        "brief_sections": [
            "Intake Notes: capture the core brain dump or link to the source.",
            "Intent summary: describe the high-level goal in a few sentences.",
        ],
        "brief_section_stubs": [
            {
                "title": "Intake Notes",
                "prompt": "Capture the core brain dump or link to the source.",
            },
            {
                "title": "Intent Summary",
                "prompt": "Describe the high-level goal in a few sentences.",
            },
        ],
    },
    "refactor": {
        "goal": "Improve the structure of the targeted area while preserving existing behavior.",
        "acceptance_criteria": [
            "The targeted code path is simpler, clearer, or better factored after the change.",
            "Behavior remains unchanged for the intended surface area.",
            "Focused verification demonstrates no regression in the refactored path.",
        ],
        "constraints": [
            "Avoid broad opportunistic cleanup outside the chosen seam.",
            "Preserve existing behavior unless the task explicitly includes functional changes.",
        ],
        "plan": [
            "Identify the narrow seam to refactor and the behavior that must stay stable.",
            "Restructure the code in small, reviewable steps.",
            "Run focused verification to confirm behavior is preserved.",
        ],
        "prompt_guidance": [
            "Name the seam being refactored and the behavior that must not change.",
            "Keep the scope structural unless the task explicitly includes functional change.",
            "Use focused verification to prove behavior stayed stable.",
        ],
        "brief_sections": [
            "Refactor seam: identify the module, function, or flow being reshaped.",
            "Behavior to preserve: list the user-visible or contract-level behavior that must stay the same.",
            "Verification: capture the checks that confirm the refactor did not regress behavior.",
        ],
        "brief_section_stubs": [
            {
                "title": "Refactor Seam",
                "prompt": "Identify the module, function, or flow being reshaped.",
            },
            {
                "title": "Behavior to Preserve",
                "prompt": "List the user-visible or contract-level behavior that must stay the same.",
            },
            {
                "title": "Verification",
                "prompt": "Capture the checks that confirm the refactor did not regress behavior.",
            },
        ],
    },
}


@dataclass(slots=True)
class BlockedTask:
    task_id: str
    title: str
    queue_position: int
    blocked_by: list[str]


@dataclass(slots=True)
class TaskSelection:
    task: TaskRecord | None
    blocked: list[BlockedTask]


@dataclass(slots=True)
class TaskPlan:
    tasks: list[TaskRecord]
    blocked: list[BlockedTask]


@dataclass(slots=True)
class WorkspaceRepairSummary:
    mutated: bool = False
    stale_runner_recovered: bool = False
    cleared_active_task_id: str | None = None
    requeued_task_ids: list[str] = field(default_factory=list)
    removed_queue_entries: list[str] = field(default_factory=list)
    deduped_queue_entries: list[str] = field(default_factory=list)
    restored_queue_entries: list[str] = field(default_factory=list)
    finalized_commit_task_ids: list[str] = field(default_factory=list)
    stale_process_task_ids: list[str] = field(default_factory=list)
    inactivity_recovered_task_ids: list[str] = field(default_factory=list)


class WorkspaceConflictError(ValueError):
    """Raised when workspace mutations would conflict with an active runner."""


@dataclass(slots=True)
class StopTaskSummary:
    task: TaskRecord
    runner_pid: int | None = None
    signal_sent: bool = False


@dataclass(slots=True)
class SwitchTaskSummary:
    task: TaskRecord
    previous_engine: str
    new_engine: str
    was_active: bool = False
    runner_pid: int | None = None
    signal_sent: bool = False
    prior_work_paths: list[str] = field(default_factory=list)


def normalize_acceptance_criteria(items: list[str] | None) -> list[str]:
    if not items:
        return []

    normalized: list[str] = []
    for item in items:
        criterion = item.strip()
        if not criterion:
            continue
        normalized.append(criterion)
    return normalized


def normalize_task_text_list(items: list[str] | None) -> list[str]:
    return normalize_acceptance_criteria(items)


def extract_report_line(text: str, key: str) -> str | None:
    match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def extract_report_list_section(text: str, key: str) -> list[str]:
    items: list[str] = []
    capture = False
    header = f"{key}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == header:
            capture = True
            continue
        if capture and re.match(r"^[A-Z_]+:", stripped):
            break
        if capture and line.lstrip().startswith("- "):
            items.append(line.split("- ", 1)[1].strip())
    return items


def normalize_human_checkpoints(items: list[str] | None) -> list[str]:
    if not items:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        checkpoint = item.strip()
        if not checkpoint:
            continue
        if checkpoint not in VALID_HUMAN_CHECKPOINTS:
            allowed = ", ".join(sorted(VALID_HUMAN_CHECKPOINTS))
            raise ValueError(
                f"Unsupported human checkpoint '{checkpoint}'. Expected one of: {allowed}"
            )
        if checkpoint in seen:
            continue
        seen.add(checkpoint)
        normalized.append(checkpoint)
    return normalized


def apply_task_template_defaults(task: TaskRecord) -> TaskRecord:
    if task.mode != "tasks" or task.task_type is None:
        return task

    template = TASK_TEMPLATES.get(task.task_type)
    if template is None:
        return task

    if not task.goal.strip():
        task.goal = str(template["goal"])
    if not task.acceptance_criteria:
        task.acceptance_criteria = list(template["acceptance_criteria"])  # type: ignore[arg-type]
    if not task.constraints:
        task.constraints = list(template["constraints"])  # type: ignore[arg-type]
    if not task.plan:
        task.plan = list(template["plan"])  # type: ignore[arg-type]
    return task


def task_template(task: TaskRecord) -> dict[str, object] | None:
    if task.mode != "tasks" or task.task_type is None:
        return None
    template = TASK_TEMPLATES.get(task.task_type)
    if template is None:
        return None
    return template


def task_brief_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "brief.md"


def _template_list(template: dict[str, object], key: str) -> list[str]:
    value = template.get(key, [])
    return list(value) if isinstance(value, list) else []


def _template_section_stubs(template: dict[str, object]) -> list[dict[str, str]]:
    value = template.get("brief_section_stubs", [])
    if not isinstance(value, list):
        return []

    stubs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        prompt = str(item.get("prompt", "")).strip()
        if not title or not prompt:
            continue
        stubs.append({"title": title, "prompt": prompt})
    return stubs


def render_task_brief(task: TaskRecord) -> str:
    lines = [
        f"# {task.id} {task.title}",
        "",
        f"- Mode: {task.mode}",
        f"- Task type: {task.task_type or '-'}",
        f"- PM complexity: {task.pm_complexity or '-'}",
        f"- Planned effort: {task.planned_effort or '-'}",
        "",
        "## Goal",
        task.goal or task.title,
        "",
        "## Acceptance Criteria",
    ]
    if task.acceptance_criteria:
        lines.extend(f"- {item}" for item in task.acceptance_criteria)
    else:
        lines.append("- No acceptance criteria defined.")

    lines.extend(["", "## Constraints"])
    if task.constraints:
        lines.extend(f"- {item}" for item in task.constraints)
    else:
        lines.append("- Keep changes scoped to the task.")

    lines.extend(["", "## Plan"])
    if task.plan:
        lines.extend(f"- {item}" for item in task.plan)
    else:
        lines.append("- No plan defined.")

    lines.extend(["", "## PM Sizing"])
    lines.append(f"- Complexity: {task.pm_complexity or 'Not estimated.'}")
    lines.append(f"- Planned effort: {task.planned_effort or 'Not sized.'}")

    template = task_template(task)
    if template is not None:
        lines.extend(["", "## Template Guidance"])
        lines.extend(f"- {item}" for item in _template_list(template, "prompt_guidance"))
        lines.extend(["", "## Intake Notes"])
        section_stubs = _template_section_stubs(template)
        if section_stubs:
            for stub in section_stubs:
                lines.extend(
                    [
                        "",
                        f"### {stub['title']}",
                        f"- {stub['prompt']}",
                        "",
                        "_TBD_",
                    ]
                )
        else:
            lines.extend(f"- {item}" for item in _template_list(template, "brief_sections"))

    return "\n".join(lines) + "\n"


def task_requires_acceptance_criteria(task: TaskRecord) -> bool:
    return bool(_acceptance_criteria_requirement_signals(task))


def missing_acceptance_criteria_reason(task: TaskRecord) -> str | None:
    if task.acceptance_criteria:
        return None
    signals = _acceptance_criteria_requirement_signals(task)
    if not signals:
        return None
    return (
        "Structured acceptance criteria are required before implementation for larger tasks. "
        f"Add at least one criterion because this task has: {', '.join(signals)}."
    )


def missing_acceptance_criteria_cli_warning(task: TaskRecord) -> str | None:
    reason = missing_acceptance_criteria_reason(task)
    if reason is None:
        return None
    return (
        f"{reason} This task will stay in `grooming` until criteria are added. "
        "Use `--acceptance-criteria` to persist at least one structured bullet."
    )


def implementation_entry_stage(task: TaskRecord) -> str:
    if getattr(task, "pipeline_mode", "full") == "single":
        return "implementing"
    if missing_acceptance_criteria_reason(task) is not None:
        return "grooming"
    return "implementing"


def needs_normalization(task: TaskRecord) -> str | None:
    """Return a reason if the task is underspecified and needs planner normalization before retry.

    Tasks already at backlog or grooming will naturally go through planner,
    so normalization only applies to tasks past grooming that lack meaningful
    acceptance criteria.

    Single-mode tasks skip normalization entirely since they have no grooming stage.
    """
    if getattr(task, "pipeline_mode", "full") == "single":
        return None
    if task.pipeline_status in {"backlog", "grooming"}:
        return None
    if task.acceptance_criteria:
        return None
    reasons = ["missing acceptance criteria"]
    if not task.goal.strip():
        reasons.append("missing goal")
    return f"Task is underspecified ({', '.join(reasons)}) and needs planner normalization before retry."


def reroute_stage_for_acceptance_criteria(task: TaskRecord) -> str:
    if getattr(task, "pipeline_mode", "full") == "single":
        return task.pipeline_status
    if task.pipeline_status in {"implementing", "testing", "accepting", "commit_to_git"}:
        if missing_acceptance_criteria_reason(task) is not None:
            return "grooming"
        return task.pipeline_status
    return task.pipeline_status


def _acceptance_criteria_requirement_signals(task: TaskRecord) -> list[str]:
    signals: list[str] = []
    if task.depends_on:
        signals.append("dependencies")
    if task.goal.strip() and task.goal.strip() != task.title.strip():
        signals.append("an explicit goal")
    if task.priority == "high":
        signals.append("high priority")
    if len(task.plan) >= 2:
        signals.append("a multi-step plan")
    return signals


def infer_acceptance_criteria(task: TaskRecord) -> list[str]:
    if task.acceptance_criteria:
        return list(task.acceptance_criteria)

    inferred: list[str] = []
    anchored = False
    goal = task.goal.strip()
    if goal and goal != task.title.strip():
        inferred.append(f"The delivered change achieves the stated goal: {goal}.")
        anchored = True

    if task.plan:
        if len(task.plan) == 1:
            inferred.append(f"The implementation completes the planned work: {task.plan[0]}.")
        else:
            inferred.append(
                "The implementation covers the defined plan end-to-end without broad unrelated changes."
            )
        anchored = True

    if task.depends_on and anchored:
        dependency_list = ", ".join(task.depends_on)
        inferred.append(
            f"The result aligns with the prerequisite task context needed from: {dependency_list}."
        )

    if not inferred:
        return []

    inferred.append("Focused verification demonstrates the targeted behavior works as intended.")
    return normalize_acceptance_criteria(inferred)


def load_state(root: Path) -> WorkspaceState:
    ensure_workspace(root)
    data = yaml.safe_load(state_path(root).read_text(encoding="utf-8")) or {}
    return WorkspaceState(**data)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _atomic_write_gzip_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with gzip.open(temp_path, "wt", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_atomic_files(writes: dict[Path, str]) -> None:
    snapshots = {
        path: path.read_text(encoding="utf-8") if path.exists() else _MISSING for path in writes
    }
    applied: list[Path] = []
    try:
        for path, content in writes.items():
            _atomic_write_text(path, content)
            applied.append(path)
    except Exception:
        for path in reversed(applied):
            previous = snapshots[path]
            if previous is _MISSING:
                if path.exists():
                    path.unlink()
                continue
            _atomic_write_text(path, previous)
        raise


def _serialize_state(state: WorkspaceState) -> str:
    return yaml.safe_dump(state.model_dump(mode="python"), sort_keys=False)


def save_state(root: Path, state: WorkspaceState) -> None:
    with workspace_mutation_guard(root):
        _atomic_write_text(state_path(root), _serialize_state(state))


def _save_state_without_runner_guard(root: Path, state: WorkspaceState) -> None:
    _atomic_write_text(state_path(root), _serialize_state(state))


def set_pool_stop_reason(root: Path, stop_reason: str | None) -> WorkspaceState:
    with _workspace_lock(root):
        state = load_state(root)
        state.pool_stop_reason = stop_reason
        save_state(root, state)
        return state


def tasks_root(root: Path) -> Path:
    ensure_workspace(root)
    return workspace_dir(root) / "tasks"


def runner_lock_path(root: Path) -> Path:
    ensure_workspace(root)
    return workspace_dir(root) / ".runner.lock"


@contextmanager
def _workspace_lock(root: Path):
    lock_path = workspace_dir(root) / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_runner_lock_metadata(handle: TextIO, status: RunnerStatusState) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(yaml.safe_dump(status.model_dump(mode="python"), sort_keys=False))
    handle.flush()


def _read_runner_lock_metadata(root: Path) -> RunnerStatusState:
    lock_path = runner_lock_path(root)
    if not lock_path.exists():
        return RunnerStatusState()
    data = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return RunnerStatusState()
    return RunnerStatusState(**data)


def _runner_metadata_present(status: RunnerStatusState) -> bool:
    return any(
        (
            status.pid is not None,
            bool(status.workspace),
            bool(status.command),
            status.started_at is not None,
            status.heartbeat_at is not None,
            status.active_task_id is not None,
        )
    )


def _runner_lock_is_active(root: Path) -> bool:
    root = root.resolve()
    if root in _RUNNER_LOCKS:
        return True
    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def _runner_status_needs_reconciliation(root: Path) -> bool:
    state = load_state(root)
    if state.active_task_id is not None:
        return True
    return any(task.runtime.execution_status == "running" for task in list_tasks(root))


def _clear_runner_lock_metadata(root: Path) -> None:
    root = root.resolve()
    if root in _RUNNER_LOCKS:
        return
    lock_path = runner_lock_path(root)
    if not lock_path.exists():
        return
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        handle.seek(0)
        handle.truncate()
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _heartbeat_is_late(heartbeat_at: str | None) -> bool:
    if heartbeat_at is None:
        return False
    try:
        from datetime import UTC, datetime

        ts = datetime.fromisoformat(heartbeat_at)
        age = (datetime.now(UTC) - ts).total_seconds()
        return age > HEARTBEAT_LATE_THRESHOLD_SECONDS
    except (ValueError, TypeError):
        return False


def runner_status(root: Path) -> RunnerStatusState:
    root = root.resolve()
    status = _read_runner_lock_metadata(root)
    if _runner_lock_is_active(root):
        if _heartbeat_is_late(status.heartbeat_at):
            return status.model_copy(update={"status": "late"})
        return status.model_copy(update={"status": "running"})
    if not _runner_metadata_present(status):
        return RunnerStatusState()
    if _runner_status_needs_reconciliation(root):
        return status.model_copy(update={"status": "stale"})
    _clear_runner_lock_metadata(root)
    return RunnerStatusState()


def _touch_runner_status(
    root: Path,
    *,
    active_task_id: str | None | object = _MISSING,
) -> None:
    root = root.resolve()
    lock_state = _RUNNER_LOCKS.get(root)
    if lock_state is None:
        return
    with lock_state.metadata_lock:
        lock_state.status.heartbeat_at = utcnow()
        lock_state.status.status = "running"
        if active_task_id is not _MISSING:
            lock_state.status.active_task_id = active_task_id
        _write_runner_lock_metadata(lock_state.handle, lock_state.status)


@contextmanager
def runner_heartbeat(
    root: Path,
    *,
    active_task_id: str | None = None,
    interval_seconds: float = 1.0,
):
    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.wait(interval_seconds):
            _touch_runner_status(root, active_task_id=active_task_id)

    _touch_runner_status(root, active_task_id=active_task_id)
    thread = threading.Thread(target=_heartbeat_loop, name="litehive-runner-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=max(interval_seconds, 0.1) * 2)
        _touch_runner_status(root, active_task_id=None)


def _current_thread_owns_runner_guard(root: Path) -> bool:
    root = root.resolve()
    owner_thread_id = threading.get_ident()
    with _RUNNER_LOCKS_MUTEX:
        existing = _RUNNER_LOCKS.get(root)
    return existing is not None and existing.owner_thread_id == owner_thread_id


def _runner_pid_is_alive(pid: object) -> bool:
    try:
        candidate = int(pid)
    except (TypeError, ValueError):
        return False
    if candidate <= 0:
        return False
    try:
        os.kill(candidate, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _subagent_process_is_stale(task: "TaskRecord") -> bool:
    """Return True if the task has a recorded subagent PID that is no longer alive."""
    if task.runtime.execution_status != "running":
        return False
    active = task.runtime.active_subagent
    if active is None or active.pid is None:
        return False
    return not _runner_pid_is_alive(active.pid)


def _runner_lock_pid_is_stale(root: Path) -> bool:
    """Return True if the runner lock metadata records a PID that is no longer alive."""
    metadata = _read_runner_lock_metadata(root)
    pid = metadata.pid
    if pid is None:
        return False
    return not _runner_pid_is_alive(pid)


def _runner_lock_is_held(root: Path) -> bool:
    if _current_thread_owns_runner_guard(root):
        return True
    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def _runner_conflict_message(root: Path) -> str:
    metadata = _read_runner_lock_metadata(root)
    pid = metadata.pid
    started_at = metadata.started_at
    heartbeat_at = metadata.heartbeat_at
    command = metadata.command
    details = []
    if pid is not None:
        details.append(f"pid={pid}")
    if started_at:
        details.append(f"started_at={started_at}")
    if heartbeat_at:
        details.append(f"heartbeat_at={heartbeat_at}")
    if command:
        details.append(f"command={command}")
    suffix = f" ({', '.join(details)})" if details else ""
    return (
        f"workspace is already being mutated by another runner{suffix}. "
        "Wait for the active run to finish before changing this workspace."
    )


@contextmanager
def workspace_runner_guard(root: Path):
    root = root.resolve()
    owner_thread_id = threading.get_ident()
    with _RUNNER_LOCKS_MUTEX:
        existing = _RUNNER_LOCKS.get(root)
        if existing is not None:
            if existing.owner_thread_id != owner_thread_id:
                raise WorkspaceConflictError(_runner_conflict_message(root))
            existing.depth += 1
    if existing is not None:
        try:
            yield
        finally:
            with _RUNNER_LOCKS_MUTEX:
                lock_state = _RUNNER_LOCKS[root]
                if lock_state.depth <= 1:
                    _RUNNER_LOCKS.pop(root, None)
                    should_close = True
                else:
                    lock_state.depth -= 1
                    should_close = False
            if should_close:
                handle = lock_state.handle
                handle.seek(0)
                handle.truncate()
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        return

    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceConflictError(_runner_conflict_message(root)) from exc
        now = utcnow()
        status = RunnerStatusState(
            status="running",
            pid=os.getpid(),
            workspace=str(root),
            command=" ".join(sys.argv),
            started_at=now,
            heartbeat_at=now,
        )
        _write_runner_lock_metadata(handle, status)
        with _RUNNER_LOCKS_MUTEX:
            _RUNNER_LOCKS[root] = _RunnerLockState(
                handle=handle, depth=1, status=status, owner_thread_id=owner_thread_id
            )
    except Exception:
        handle.close()
        raise

    try:
        yield
    finally:
        with _RUNNER_LOCKS_MUTEX:
            _RUNNER_LOCKS.pop(root, None)
        handle.seek(0)
        handle.truncate()
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def workspace_mutation_guard(root: Path):
    root = root.resolve()
    owner_thread_id = threading.get_ident()
    with _RUNNER_LOCKS_MUTEX:
        existing = _RUNNER_LOCKS.get(root)
    if existing is not None and existing.owner_thread_id == owner_thread_id:
        yield
        return
    with workspace_runner_guard(root):
        yield


def _ensure_future_task_mutation_allowed(
    root: Path,
    task_ids: list[str],
    *,
    state: WorkspaceState | None = None,
) -> None:
    markers = active_task_markers(root, state)
    conflicts: list[str] = []
    for task_id in task_ids:
        if task_id not in markers:
            continue
        task = get_task(root, task_id)
        marker_set = set(markers[task_id])
        if (
            marker_set == {"workspace.active_task_id"}
            and task is not None
            and not _is_task_eligible_for_execution(task)
            and task.runtime.execution_status != "running"
        ):
            continue
        conflicts.append(f"{task_id} ({', '.join(markers[task_id])})")
    if conflicts:
        details = "; ".join(conflicts)
        raise WorkspaceConflictError(
            f"runner is actively using task state that cannot be changed concurrently: {details}"
        )


def _persist_future_task_update(
    root: Path,
    task: TaskRecord,
    *,
    journal_message: str | None = None,
) -> None:
    task.updated_at = utcnow()
    writes = {
        task_file(root, task): _serialize_task_record(task),
        task_runtime_file(root, task): _serialize_task_runtime(task),
    }
    if journal_message is not None:
        journal_path = task_dir(root, task) / "journal.md"
        existing = journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
        writes[journal_path] = f"{existing}\n## {utcnow()}\n{journal_message}\n"
    if task.mode == "tasks":
        writes[task_brief_file(root, task)] = render_task_brief(task)
    _write_atomic_files(writes)
    _ensure_runtime_ignored(root)


def slugify(value: str, max_length: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        return "task"
    if len(slug) <= max_length:
        return slug
    truncated = slug[:max_length].rsplit("-", 1)[0]
    return truncated.strip("-") or slug[:max_length].strip("-")


def _highest_task_number_on_disk(root: Path) -> int:
    existing = []
    for child in tasks_root(root).iterdir():
        if not child.is_dir():
            continue
        match = re.match(r"^T-(\d{4})-", child.name)
        if match:
            existing.append(int(match.group(1)))
    return max(existing, default=0)


def _reserve_next_task_numbers(root: Path, state: WorkspaceState, *, count: int = 1) -> list[int]:
    if count < 1:
        raise ValueError("count must be 1 or greater")
    if state.next_task_number <= 0:
        state.next_task_number = _highest_task_number_on_disk(root)
    start = state.next_task_number + 1
    state.next_task_number += count
    return list(range(start, start + count))


def task_dir(root: Path, task: TaskRecord) -> Path:
    return tasks_root(root) / f"{task.id}-{task.slug}"


def task_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "task.yaml"


def task_runtime_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "runtime.yaml"


def task_thread_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "thread.yaml"


def task_recovery_dir(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "recovery"


def _latest_path(paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    return sorted(existing)[-1]


def _artifact_candidates(base: Path, *names: str) -> list[Path]:
    candidates: list[Path] = []
    for name in names:
        path = base / name
        candidates.append(path)
        if path.suffix != ".gz":
            candidates.append(base / f"{name}.gz")
    return candidates


def _resolve_artifact_path(base: Path, *names: str) -> Path | None:
    for candidate in _artifact_candidates(base, *names):
        if candidate.exists():
            return candidate
    return None


def _read_text_artifact(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8")


def _latest_run_all_log_path(root: Path) -> Path | None:
    logs_root = root / ".litehive" / "logs" / "run-all"
    if not logs_root.exists():
        return None
    candidates = [
        path
        for path in logs_root.rglob("*")
        if path.is_file() and (path.suffix == ".log" or path.name.endswith(".log.gz"))
    ]
    if not candidates:
        return None
    return sorted(candidates)[-1]


def _latest_subagent_base(root: Path, task: TaskRecord) -> Path | None:
    refs = list(task.subagents)
    preferred = []
    if task.runtime.active_subagent is not None:
        preferred.append(task.runtime.active_subagent.path)
    if task.runtime.last_subagent is not None:
        preferred.append(task.runtime.last_subagent.path)
    preferred.extend(ref.path for ref in refs)
    for rel_path in reversed(preferred):
        base = task_dir(root, task) / rel_path
        if base.exists():
            return base
    subagents_root = task_dir(root, task) / "subagents"
    if not subagents_root.exists():
        return None
    candidates = [path for path in subagents_root.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return sorted(candidates)[-1]


def _status_entry_paths(entries: list[str]) -> list[str]:
    paths: list[str] = []
    for entry in entries:
        stripped = entry.strip()
        if not stripped:
            continue
        if " -> " in stripped:
            stripped = stripped.split(" -> ", 1)[1]
        if len(stripped) > 3:
            stripped = stripped[3:]
        paths.append(stripped)
    return paths


def collect_recovery_evidence(
    root: Path,
    task: TaskRecord,
    *,
    stage: str | None = None,
) -> list[RecoveryEvidenceItem]:
    from litehive.observability import engine_monitoring_file, load_engine_monitoring

    evidence: list[RecoveryEvidenceItem] = []
    task_path = task_file(root, task)
    runtime_path = task_runtime_file(root, task)
    thread_path = task_thread_file(root, task)
    events_path = task_dir(root, task) / "events.jsonl"
    latest_report_path = _latest_path(sorted((task_dir(root, task) / "reports").glob("*.yaml")))
    latest_run_log = _latest_run_all_log_path(root)
    monitoring_path = engine_monitoring_file(root)
    monitoring = load_engine_monitoring(root)
    engine_record = monitoring.engines.get(task.engine or "")
    subagent_base = _latest_subagent_base(root, task)

    evidence.append(
        RecoveryEvidenceItem(
            kind="task",
            label="task.yaml",
            path=str(task_path.relative_to(root)),
            exists=task_path.exists(),
            summary=f"status={task.status} pipeline_status={task.pipeline_status} priority={task.priority}",
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="runtime",
            label="runtime.yaml",
            path=str(runtime_path.relative_to(root)),
            exists=runtime_path.exists(),
            summary=(
                f"execution_status={task.runtime.execution_status} current_stage={task.runtime.current_stage.step} "
                f"last_outcome={task.runtime.last_outcome.kind or 'none'}"
            ),
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="thread",
            label="thread.yaml",
            path=str(thread_path.relative_to(root)),
            exists=thread_path.exists(),
            summary=f"discussion entries={len(load_task_thread(root, task))}",
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="events",
            label="events.jsonl",
            path=str(events_path.relative_to(root)),
            exists=events_path.exists(),
            summary="task lifecycle and subagent event stream",
        )
    )
    if latest_report_path is not None:
        evidence.append(
            RecoveryEvidenceItem(
                kind="stage_report",
                label="latest stage report",
                path=str(latest_report_path.relative_to(root)),
                exists=True,
                summary=f"latest report for {task.id}",
            )
        )
    if subagent_base is not None:
        for name, label in (
            ("session.yaml", "latest subagent session"),
            ("report.yaml", "latest subagent report"),
            ("transcript.md", "latest subagent transcript"),
            ("stdout.txt", "latest subagent stdout"),
            ("stderr.txt", "latest subagent stderr"),
            ("timeline.yaml", "latest subagent events timeline"),
        ):
            path = _resolve_artifact_path(subagent_base, name)
            display_path = path if path is not None else subagent_base / name
            evidence.append(
                RecoveryEvidenceItem(
                    kind="subagent_artifact",
                    label=label,
                    path=str(display_path.relative_to(root)),
                    exists=path is not None,
                    summary=f"artifact from {subagent_base.name}",
                )
            )
    if latest_run_log is not None:
        evidence.append(
            RecoveryEvidenceItem(
                kind="wrapper_log",
                label="latest run-all log",
                path=str(latest_run_log.relative_to(root)),
                exists=True,
                summary="latest daemon/run-all wrapper log",
            )
        )
    evidence.append(
        RecoveryEvidenceItem(
            kind="engine_monitoring",
            label="engine-monitoring.yaml",
            path=str(monitoring_path.relative_to(root)),
            exists=monitoring_path.exists(),
            summary=(
                "no engine record"
                if engine_record is None
                else (
                    f"engine={engine_record.engine} invocations={engine_record.invocation_count} "
                    f"failures={engine_record.failure_count} limits={engine_record.limit_event_count}"
                )
            ),
        )
    )

    if is_git_repo(root):
        worktree_rel = get_task_worktree_path(task)
        worktree_path = root / worktree_rel if worktree_rel else None
        try:
            root_status = status_porcelain(root)
        except GitError:
            root_status = []
        worktree_status: list[str] = []
        if worktree_path is not None and worktree_path.exists():
            try:
                worktree_status = status_porcelain(worktree_path)
            except GitError:
                worktree_status = []
        evidence.append(
            RecoveryEvidenceItem(
                kind="git",
                label="main checkout git state",
                exists=True,
                summary=f"head={current_head(root) or 'missing'} dirty={len(root_status)}",
                metadata={"dirty_paths": _status_entry_paths(root_status)},
            )
        )
        evidence.append(
            RecoveryEvidenceItem(
                kind="worktree",
                label="task worktree state",
                path=worktree_rel,
                exists=worktree_path.exists() if worktree_path is not None else False,
                summary=(
                    "task worktree not configured"
                    if worktree_path is None
                    else f"dirty={len(worktree_status)}"
                ),
                metadata={"dirty_paths": _status_entry_paths(worktree_status), "stage": stage},
            )
        )
    return evidence


def write_recovery_report(root: Path, task: TaskRecord, report: RecoveryReport) -> Path:
    reports_dir = task_recovery_dir(root, task)
    reports_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(reports_dir.glob("recovery-*.yaml"))
    ordinal = len(existing) + 1
    path = reports_dir / f"recovery-{ordinal:03d}.yaml"
    path.write_text(
        yaml.safe_dump(report.model_dump(mode="python"), sort_keys=False), encoding="utf-8"
    )
    return path


def record_recovery_report(
    root: Path,
    task: TaskRecord,
    *,
    trigger: str,
    stage: str | None,
    summary: str,
    runnable_state: str,
    actions: list[RecoveryAction] | None = None,
    failure_classification: str | None = None,
    blocker: str | None = None,
    warnings: list[str] | None = None,
    recovery_subagent_id: str | None = None,
    recovery_subagent_path: str | None = None,
) -> Path:
    from litehive.models import TaskThreadComment

    report = RecoveryReport(
        task_id=task.id,
        stage=stage,
        trigger=trigger,
        summary=summary,
        failure_classification=failure_classification,
        runnable_state=runnable_state,  # type: ignore[arg-type]
        blocker=blocker,
        evidence=collect_recovery_evidence(root, task, stage=stage),
        actions=list(actions or []),
        warnings=list(warnings or []),
        recovery_subagent_id=recovery_subagent_id,
        recovery_subagent_path=recovery_subagent_path,
    )
    path = write_recovery_report(root, task, report)
    append_thread_comment(
        root,
        task,
        TaskThreadComment(
            role="recovery",
            step=stage or task.pipeline_status,
            verdict="comment",
            message=(
                f"Recovery trigger `{trigger}`: {summary}\n"
                f"runnable_state: {runnable_state}\n"
                f"report: {path.relative_to(root)}" + (f"\nblocker: {blocker}" if blocker else "")
            ),
        ),
    )
    return path


def append_thread_comment(root: Path, task: TaskRecord, comment: "TaskThreadComment") -> None:
    from litehive.models import TaskThreadComment  # noqa: F811

    path = task_thread_file(root, task)
    existing: list[dict] = []
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            existing = loaded
    existing.append(comment.model_dump(mode="python"))
    path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")


def load_task_thread(root: Path, task: TaskRecord) -> list["TaskThreadComment"]:
    from litehive.models import TaskThreadComment

    path = task_thread_file(root, task)
    if not path.exists():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        return []
    return [TaskThreadComment(**entry) for entry in loaded if isinstance(entry, dict)]


def render_task_thread(root: Path, task: TaskRecord) -> str:
    thread = load_task_thread(root, task)
    if not thread:
        return ""
    lines = ["Discussion thread:"]
    for c in thread:
        header = f"[{c.created_at}] {c.role} ({c.step}) — {c.verdict}"
        lines.append(f"\n--- {header} ---")
        lines.append(c.message)
        if c.files_changed:
            lines.append(f"Files: {', '.join(c.files_changed)}")
    return "\n".join(lines)


def _ensure_runtime_ignored(root: Path) -> None:
    ignore_path = workspace_gitignore_path(root)
    expected = render_workspace_gitignore()
    if not ignore_path.exists() or ignore_path.read_text(encoding="utf-8") != expected:
        ignore_path.write_text(expected, encoding="utf-8")


def _serialize_task_record(task: TaskRecord) -> str:
    _normalize_task_worktree_state(task)
    payload = task.model_dump(mode="python")
    payload["git"]["worktree_path"] = None
    return yaml.safe_dump(payload, sort_keys=False)


def _serialize_task_runtime(task: TaskRecord) -> str:
    _normalize_task_worktree_state(task)
    return yaml.safe_dump(
        {
            **task.runtime.model_dump(mode="python"),
            "git": {
                "commit_sha": task.git.commit_sha,
                "worktree_path": task.runtime.git.worktree_path,
            },
        },
        sort_keys=False,
    )


def _write_task_runtime(root: Path, task: TaskRecord) -> None:
    _atomic_write_text(task_runtime_file(root, task), _serialize_task_runtime(task))
    _ensure_runtime_ignored(root)


def set_task_commit_sha(task: TaskRecord, commit_sha: str | None) -> None:
    task.git.commit_sha = commit_sha
    task.runtime.git.commit_sha = commit_sha


def get_task_worktree_path(task: TaskRecord) -> str | None:
    return task.runtime.git.worktree_path or task.git.worktree_path


def set_task_worktree_path(task: TaskRecord, worktree_path: str | None) -> None:
    task.runtime.git.worktree_path = worktree_path
    task.git.worktree_path = None


def clear_task_worktree_path(task: TaskRecord) -> None:
    set_task_worktree_path(task, None)


def _normalize_task_worktree_state(task: TaskRecord) -> None:
    if task.runtime.git.worktree_path:
        task.git.worktree_path = None
        return
    if task.git.worktree_path:
        set_task_worktree_path(task, task.git.worktree_path)


def save_task_runtime(root: Path, task: TaskRecord) -> None:
    with workspace_mutation_guard(root):
        _write_task_runtime(root, task)


def _load_task_runtime(root: Path, task: TaskRecord) -> TaskRecord:
    runtime_file = task_runtime_file(root, task)
    if not runtime_file.exists():
        _normalize_task_worktree_state(task)
        return task
    data = yaml.safe_load(runtime_file.read_text(encoding="utf-8")) or {}
    task.runtime = TaskRuntime(**data)
    set_task_commit_sha(task, task.runtime.git.commit_sha)
    _normalize_task_worktree_state(task)
    return task


def _load_task_record_file(path: Path) -> TaskRecord:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return TaskRecord(**data)


def create_task(
    root: Path,
    *,
    title: str,
    depends_on: list[str] | None = None,
    mode: str = "implementation",
    pipeline_mode: str = "full",
    task_type: str | None = None,
    engine: str | None = None,
    model: str | None = None,
    retry_limit: int | None = None,
    goal: str = "",
    acceptance_criteria: list[str] | None = None,
    pm_complexity: str | None = None,
    planned_effort: str | None = None,
    human_checkpoints: list[str] | None = None,
    auto_commit: bool = True,
    upstream_origin: UpstreamContributionOrigin | None = None,
    priority: str | None = None,
) -> TaskRecord:
    ensure_workspace(root)
    if retry_limit is not None and retry_limit < 0:
        raise ValueError("Retry limit must be 0 or greater")
    if pipeline_mode not in {"single", "full"}:
        raise ValueError(f"Unsupported pipeline_mode '{pipeline_mode}'")
    if priority is not None and priority not in VALID_TASK_PRIORITIES:
        raise ValueError(f"Unsupported priority '{priority}'; choose from {sorted(VALID_TASK_PRIORITIES)}")
    if task_type is not None and task_type not in VALID_TASK_TYPES:
        raise ValueError(f"Unsupported task type '{task_type}'")
    if pm_complexity is not None and pm_complexity not in VALID_PM_COMPLEXITIES:
        raise ValueError(f"Unsupported PM complexity '{pm_complexity}'")
    if planned_effort is not None and planned_effort not in VALID_PLANNED_EFFORTS:
        raise ValueError(f"Unsupported planned effort '{planned_effort}'")
    if priority is not None and priority not in VALID_TASK_PRIORITIES:
        raise ValueError(f"Unsupported priority '{priority}'")
    with _workspace_lock(root):
        state = load_state(root)
        task_id = f"T-{_reserve_next_task_numbers(root, state)[0]:04d}"
        slug = slugify(title)
        _validate_task_dependencies(root, task_id=task_id, depends_on=depends_on or [])
        task = TaskRecord(
            id=task_id,
            slug=slug,
            title=title,
            depends_on=list(depends_on or []),
            task_type=task_type,
            engine=engine,
            model=model,
            mode=mode,  # type: ignore[arg-type]
            pipeline_mode=pipeline_mode,  # type: ignore[arg-type]
            priority=priority or "medium",
            goal=goal,
            acceptance_criteria=normalize_acceptance_criteria(acceptance_criteria),
            pm_complexity=pm_complexity,  # type: ignore[arg-type]
            planned_effort=planned_effort,  # type: ignore[arg-type]
            human_checkpoints=normalize_human_checkpoints(human_checkpoints),
            retry_policy={"max_retries": retry_limit},
            git={
                "auto_commit": auto_commit,
                "commit_message": default_commit_message(task_id, slug),
            },
            upstream_origin=upstream_origin,
        )
        task = apply_task_template_defaults(task)

        base = task_dir(root, task)
        (base / "reports").mkdir(parents=True, exist_ok=False)
        (base / "subagents").mkdir(parents=True, exist_ok=False)
        (base / "artifacts").mkdir(parents=True, exist_ok=False)
        state.queue.append(task.id)
        try:
            state = _merged_state_for_runner_owned_write(
                root,
                state=state,
                protected_task_ids=[task.id],
            )
            writes = {
                task_file(root, task): yaml.safe_dump(
                    task.model_dump(mode="python"), sort_keys=False
                ),
                task_runtime_file(root, task): _serialize_task_runtime(task),
                base / "journal.md": f"# {task.id} {task.title}\n\n## {utcnow()}\nTask created.\n",
                state_path(root): _serialize_state(state),
            }
            if task.mode == "tasks":
                writes[task_brief_file(root, task)] = render_task_brief(task)
            _write_atomic_files(writes)
        except Exception:
            shutil.rmtree(base, ignore_errors=True)
            raise
        _ensure_runtime_ignored(root)
        return task


def create_follow_up_tasks(
    root: Path,
    *,
    parent_task: TaskRecord,
    stage: str,
    follow_ups: list[FollowUpTaskSpec],
) -> list[TaskRecord]:
    if not follow_ups:
        return []
    if stage not in {"grooming", "accepting"}:
        return []

    ensure_workspace(root)
    created_tasks: list[TaskRecord] = []
    created_dirs: list[Path] = []
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        reserved_numbers = _reserve_next_task_numbers(root, state, count=len(follow_ups))
        writes: dict[Path, str] = {}

        for next_number, follow_up in zip(reserved_numbers, follow_ups):
            task_id = f"T-{next_number:04d}"
            slug = slugify(follow_up.title)
            mode = "tasks" if follow_up.task_type else "implementation"
            task = TaskRecord(
                id=task_id,
                slug=slug,
                title=follow_up.title,
                mode=mode,  # type: ignore[arg-type]
                task_type=follow_up.task_type,
                goal=follow_up.goal,
                acceptance_criteria=normalize_acceptance_criteria(follow_up.acceptance_criteria),
                created_from=TaskCreationSource(
                    task_id=parent_task.id,
                    stage=stage,  # type: ignore[arg-type]
                    rationale=follow_up.rationale,
                    blocking=follow_up.blocking,
                ),
                git={
                    "auto_commit": True,
                    "commit_message": default_commit_message(task_id, slug),
                },
            )
            task = apply_task_template_defaults(task)

            base = task_dir(root, task)
            (base / "reports").mkdir(parents=True, exist_ok=False)
            (base / "subagents").mkdir(parents=True, exist_ok=False)
            (base / "artifacts").mkdir(parents=True, exist_ok=False)
            created_dirs.append(base)
            state.queue.append(task.id)
            writes[task_file(root, task)] = yaml.safe_dump(
                task.model_dump(mode="python"), sort_keys=False
            )
            writes[task_runtime_file(root, task)] = _serialize_task_runtime(task)
            writes[base / "journal.md"] = (
                f"# {task.id} {task.title}\n\n"
                f"## {utcnow()}\n"
                "Task created.\n\n"
                f"Created as a follow-up from `{parent_task.id}` during `{stage}`.\n"
                f"Rationale: {follow_up.rationale}\n"
            )
            if task.mode == "tasks":
                writes[task_brief_file(root, task)] = render_task_brief(task)
            created_tasks.append(task)

        state = _merged_state_for_runner_owned_write(
            root,
            state=state,
            protected_task_ids=[task.id for task in created_tasks],
        )
        writes[state_path(root)] = _serialize_state(state)
        try:
            _write_atomic_files(writes)
        except Exception:
            for base in reversed(created_dirs):
                shutil.rmtree(base, ignore_errors=True)
            raise
        _ensure_runtime_ignored(root)
    return created_tasks


def discard_created_task(root: Path, task_id: str) -> None:
    with _workspace_lock(root):
        task = get_task(root, task_id)
        state = load_state(root)
        if state.active_task_id == task_id:
            state.active_task_id = None
        state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
        _save_state_without_runner_guard(root, state)
        if task is not None:
            shutil.rmtree(task_dir(root, task), ignore_errors=True)


def list_tasks(root: Path, *, include_runtime: bool = True) -> list[TaskRecord]:
    records: list[TaskRecord] = []
    for child in sorted(tasks_root(root).iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if not path.exists():
            continue
        task = _load_task_record_file(path)
        if include_runtime:
            task = _load_task_runtime(root, task)
        records.append(task)
    return records


def list_tasks_state_first(
    root: Path,
    *,
    state: WorkspaceState | None = None,
    include_runtime: bool = False,
) -> list[TaskRecord]:
    task_by_id: dict[str, TaskRecord] = {}
    for child in sorted(tasks_root(root).iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if not path.exists():
            continue
        task = _load_task_record_file(path)
        if include_runtime:
            task = _load_task_runtime(root, task)
        task_by_id[task.id] = task

    workspace_state = load_state(root) if state is None else state
    ordered_ids: list[str] = []
    seen: set[str] = set()

    def add(task_id: str | None) -> None:
        if task_id is None or task_id in seen or task_id not in task_by_id:
            return
        seen.add(task_id)
        ordered_ids.append(task_id)

    add(workspace_state.active_task_id)
    for task_id in workspace_state.queue:
        add(task_id)
    for task_id in sorted(task_by_id):
        add(task_id)

    return [task_by_id[task_id] for task_id in ordered_ids]


def get_task(root: Path, task_id: str) -> TaskRecord | None:
    for task in list_tasks(root):
        if task.id == task_id:
            return task
    return None


def require_task(root: Path, task_id: str) -> TaskRecord:
    task = get_task(root, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    return task


def save_task(root: Path, task: TaskRecord) -> None:
    task.updated_at = utcnow()
    with workspace_mutation_guard(root):
        writes = _workspace_transition_writes(root, tasks=[task])
        _write_atomic_files(writes)
        _ensure_runtime_ignored(root)


def _workspace_transition_writes(
    root: Path,
    *,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...] = (),
    state: WorkspaceState | None = None,
    journal_messages: dict[str, str] | None = None,
) -> dict[Path, str]:
    writes: dict[Path, str] = {}
    for task in tasks:
        writes[task_file(root, task)] = _serialize_task_record(task)
        writes[task_runtime_file(root, task)] = _serialize_task_runtime(task)
        if journal_messages is None or task.id not in journal_messages:
            continue
        journal_path = task_dir(root, task) / "journal.md"
        existing = journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
        writes[journal_path] = f"{existing}\n## {utcnow()}\n{journal_messages[task.id]}\n"
    if state is not None:
        state = _merged_state_for_runner_owned_write(
            root,
            state=state,
            protected_task_ids=[task.id for task in tasks],
        )
        writes[state_path(root)] = _serialize_state(state)
    return writes


def _merge_queue_preserving_future_changes(
    *,
    desired_queue: list[str],
    latest_queue: list[str],
    protected_task_ids: list[str] | tuple[str, ...],
) -> list[str]:
    protected: list[str] = []
    seen_protected: set[str] = set()
    for task_id in protected_task_ids:
        if task_id in seen_protected:
            continue
        seen_protected.add(task_id)
        protected.append(task_id)
    if not protected:
        return list(desired_queue)

    protected_set = set(protected)
    latest_unprotected = [task_id for task_id in latest_queue if task_id not in protected_set]
    protected_positions = [
        (
            sum(1 for preceding in desired_queue[:index] if preceding not in protected_set),
            task_id,
        )
        for index, task_id in enumerate(desired_queue)
        if task_id in protected_set
    ]
    if not protected_positions:
        return list(latest_unprotected)

    merged = list(latest_unprotected)
    inserted = 0
    inserted_ids: set[str] = set()
    for unprotected_before, task_id in protected_positions:
        if task_id in inserted_ids:
            continue
        insertion_index = min(unprotected_before + inserted, len(merged))
        merged.insert(insertion_index, task_id)
        inserted_ids.add(task_id)
        inserted += 1
    return merged


def _merged_state_for_runner_owned_write(
    root: Path,
    *,
    state: WorkspaceState,
    protected_task_ids: list[str] | tuple[str, ...] = (),
) -> WorkspaceState:
    latest_state = load_state(root)
    merged_state = state.model_copy(deep=True)
    merged_state.queue = _merge_queue_preserving_future_changes(
        desired_queue=state.queue,
        latest_queue=latest_state.queue,
        protected_task_ids=protected_task_ids,
    )
    merged_state.next_task_number = max(state.next_task_number, latest_state.next_task_number)
    return merged_state


def apply_task_updates_from_report(root: Path, task: TaskRecord, report: "StageReport") -> bool:
    """Apply structured and text-based task updates from a stage report."""
    from litehive.cli._parse import _parse_rich_task_update_document

    updates: dict[str, object] = {}

    # Preferred: structured task_update from STAGE_RESULT or TASK_UPDATE YAML block.
    if report.task_update:
        try:
            updates = _parse_rich_task_update_document(
                report.task_update, source="report task_update"
            )
        except ValueError as exc:
            append_journal(root, task, f"Warning: Ignoring malformed task update in report: {exc}")

    # Fallback/Supplemental: text-based list sections.
    feedback = report.feedback
    if "acceptance_criteria" not in updates:
        text_criteria = normalize_acceptance_criteria(
            extract_report_list_section(feedback, "ACCEPTANCE_CRITERIA")
        )
        if not text_criteria and not task.acceptance_criteria:
            text_criteria = infer_acceptance_criteria(task)
        if text_criteria and not task.acceptance_criteria:
            updates["acceptance_criteria"] = text_criteria

    if "constraints" not in updates:
        text_constraints = normalize_task_text_list(
            extract_report_list_section(feedback, "CONSTRAINTS")
        )
        if text_constraints and not task.constraints:
            updates["constraints"] = text_constraints

    if "plan" not in updates:
        text_plan = normalize_task_text_list(extract_report_list_section(feedback, "PLAN"))
        if text_plan and not task.plan:
            updates["plan"] = text_plan

    # Fallback/Supplemental: PM sizing lines.
    pm_complexity = extract_report_line(feedback, "PM_COMPLEXITY")
    if "pm_complexity" not in updates and pm_complexity in VALID_PM_COMPLEXITIES:
        updates["pm_complexity"] = pm_complexity

    planned_effort = extract_report_line(feedback, "PLANNED_EFFORT")
    if "planned_effort" not in updates and planned_effort in VALID_PLANNED_EFFORTS:
        updates["planned_effort"] = planned_effort

    if not updates:
        return False

    journal_message = "Task record updated from grooming output:\n" + "\n".join(
        f"- {key}: `{value}`" for key, value in updates.items()
    )

    # Use update_task logic to apply updates.
    updated = update_task(
        root,
        task.id,
        goal=updates.get("goal", ...),
        acceptance_criteria=updates.get("acceptance_criteria", ...),
        constraints=updates.get("constraints", ...),
        plan=updates.get("plan", ...),
        pm_complexity=updates.get("pm_complexity", ...),
        planned_effort=updates.get("planned_effort", ...),
        depends_on=updates.get("depends_on", ...),
        human_checkpoints=updates.get("human_checkpoints", ...),
        task_type=updates.get("task_type", ...),
        mode=updates.get("mode", ...),
        priority=updates.get("priority", ...),
        engine=updates.get("engine", ...),
        model=updates.get("model", ...),
        retry_limit=updates.get("retry_limit", ...),
        auto_commit=updates.get("auto_commit", ...),
        outcome=updates.get("outcome", ...),
        outcome_reason=updates.get("outcome_reason", ...),
        action=updates.get("action", ...),
        journal_message=journal_message,
    )

    # Re-sync the passed-in task object so the caller sees the changes.
    for field_name in updated.__class__.model_fields:
        if field_name == "runtime":
            continue
        setattr(task, field_name, getattr(updated, field_name))

    return True


def persist_task_and_state(
    root: Path,
    *,
    task: TaskRecord,
    state: WorkspaceState,
    journal_message: str | None = None,
) -> None:
    persist_tasks_and_state(
        root,
        tasks=[task],
        state=state,
        journal_messages={task.id: journal_message} if journal_message is not None else None,
    )


def persist_tasks_and_state(
    root: Path,
    *,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...],
    state: WorkspaceState,
    journal_messages: dict[str, str] | None = None,
) -> None:
    for task in tasks:
        task.updated_at = utcnow()
    writes = _workspace_transition_writes(
        root,
        tasks=tasks,
        state=state,
        journal_messages=journal_messages,
    )
    with workspace_mutation_guard(root):
        _write_atomic_files(writes)
        _ensure_runtime_ignored(root)


def _persist_tasks_and_state_without_runner_guard(
    root: Path,
    *,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...],
    state: WorkspaceState,
    journal_messages: dict[str, str] | None = None,
) -> None:
    for task in tasks:
        task.updated_at = utcnow()
    writes = _workspace_transition_writes(
        root,
        tasks=tasks,
        state=state,
        journal_messages=journal_messages,
    )
    _write_atomic_files(writes)
    _ensure_runtime_ignored(root)


def _persist_task_and_state_without_runner_guard(
    root: Path,
    *,
    task: TaskRecord,
    state: WorkspaceState,
    journal_message: str | None = None,
) -> None:
    _persist_tasks_and_state_without_runner_guard(
        root,
        tasks=[task],
        state=state,
        journal_messages={task.id: journal_message} if journal_message is not None else None,
    )


def mark_task_run_started(root: Path, task: TaskRecord) -> None:
    now = utcnow()
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = now
    task.runtime.updated_at = now
    task.runtime.retry_count = 0
    task.runtime.retry_limit = task.runtime.retry_limit
    task.runtime.last_outcome = TaskOutcomeState()
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    task.runtime.active_subagent = None
    task.runtime.interruption = None
    save_task_runtime(root, task)


def mark_task_run_finished(root: Path, task: TaskRecord, final_status: str) -> None:
    now = utcnow()
    task.runtime.execution_status = final_status
    task.runtime.updated_at = now
    task.runtime.active_subagent = None
    save_task_runtime(root, task)


def finish_task_run_transition(root: Path, task: TaskRecord, final_status: str) -> TaskRecord:
    with workspace_mutation_guard(root), _workspace_lock(root):
        now = utcnow()
        task.runtime.execution_status = final_status
        task.runtime.updated_at = now
        task.runtime.active_subagent = None
        state = load_state(root)
        state_changed = False
        if state.active_task_id == task.id:
            state.active_task_id = None
            state_changed = True
        queued_without_task = [item for item in state.queue if item != task.id]
        if queued_without_task != state.queue:
            state.queue = queued_without_task
            state_changed = True
        if (
            final_status in {"paused", "queued", "interrupted"}
            and task.status == "queued"
            and task.pipeline_status != "done"
        ):
            state.queue.insert(0, task.id)
            state_changed = True
        if (
            final_status == "done"
            and task.status == "done"
            and task.pipeline_status == "done"
            and not state_changed
        ):
            _write_task_runtime(root, task)
            return task
        persist_task_and_state(root, task=task, state=state)
        return task


def set_task_retry_state(
    root: Path,
    task: TaskRecord,
    *,
    retry_count: int,
    retry_limit: int,
    retry_source: str,
) -> None:
    _apply_task_retry_state(
        task,
        retry_count=retry_count,
        retry_limit=retry_limit,
        retry_source=retry_source,
    )
    save_task_runtime(root, task)


def clear_task_outcome(root: Path, task: TaskRecord) -> None:
    _clear_task_outcome(task)
    save_task_runtime(root, task)


def set_task_continuation_handoff(
    root: Path,
    task: TaskRecord,
    handoff: RuntimeContinuationHandoff | None,
) -> None:
    task.runtime.updated_at = utcnow()
    task.runtime.continuation_handoff = handoff
    save_task_runtime(root, task)


def clear_task_continuation_handoff(root: Path, task: TaskRecord) -> None:
    if task.runtime.continuation_handoff is None:
        return
    set_task_continuation_handoff(root, task, None)


def _apply_task_retry_state(
    task: TaskRecord,
    *,
    retry_count: int,
    retry_limit: int,
    retry_source: str,
) -> None:
    task.runtime.updated_at = utcnow()
    task.runtime.retry_count = retry_count
    task.runtime.retry_limit = retry_limit
    task.runtime.retry_source = retry_source


def _clear_task_outcome(task: TaskRecord) -> None:
    task.runtime.updated_at = utcnow()
    task.runtime.last_outcome = TaskOutcomeState()


def mark_task_outcome(
    root: Path,
    task: TaskRecord,
    *,
    kind: str,
    stage: str,
    reason_code: str,
    reason: str,
    retry_count: int,
    retry_limit: int,
    retry_source: str,
    failure_classification: str | None = None,
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] | None = None,
) -> None:
    _apply_task_outcome(
        task,
        kind=kind,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
        retry_count=retry_count,
        retry_limit=retry_limit,
        retry_source=retry_source,
        failure_classification=failure_classification,
        failure_diagnostics=failure_diagnostics,
    )
    save_task_runtime(root, task)


def _apply_task_outcome(
    task: TaskRecord,
    *,
    kind: str,
    stage: str,
    reason_code: str,
    reason: str,
    retry_count: int,
    retry_limit: int,
    retry_source: str,
    failure_classification: str | None = None,
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] | None = None,
) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.last_outcome = TaskOutcomeState(
        kind=kind,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
        failure_classification=failure_classification,
        failure_diagnostics=dict(failure_diagnostics or {}),
        retry_count=retry_count,
        retry_limit=retry_limit,
        retry_source=retry_source,
        recorded_at=now,
    )


def mark_stage_started(root: Path, task: TaskRecord, step: str) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": step,
            "status": "running",
            "started_at": now,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    save_task_runtime(root, task)


def mark_stage_finished(root: Path, task: TaskRecord, report: StageReport) -> None:
    _apply_stage_finished(task, report)
    save_task_runtime(root, task)


def _apply_stage_finished(task: TaskRecord, report: StageReport) -> None:
    now = utcnow()
    started_at = task.runtime.current_stage.started_at
    task.runtime.updated_at = now
    task.runtime.last_stage = task.runtime.last_stage.model_copy(
        update={
            "step": report.step,
            "status": "completed" if report.verdict in {"pass", "accept"} else report.verdict,
            "started_at": started_at,
            "completed_at": now,
            "updated_at": now,
            "duration_seconds": _duration_seconds(started_at, now),
            "verdict": report.verdict,
            "summary": report.summary,
        }
    )
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    if (
        task.runtime.continuation_handoff is not None
        and task.runtime.continuation_handoff.step == report.step
    ):
        task.runtime.continuation_handoff = None


def mark_subagent_started(root: Path, task: TaskRecord, ref: SubagentRef) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.active_subagent = RuntimeSubagentState(
        id=ref.id,
        role=ref.role,
        engine=ref.engine,
        status=ref.status,
        path=ref.path,
        sandboxed=ref.sandboxed,
        sandbox_summary=ref.sandbox_summary,
        started_at=now,
        updated_at=now,
        continuation=None,
    )
    save_task_runtime(root, task)


def mark_subagent_pid(root: Path, task: TaskRecord, pid: int | None) -> None:
    if (
        pid is None
        or task.runtime.active_subagent is None
        or task.runtime.active_subagent.pid == pid
    ):
        return
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.active_subagent = task.runtime.active_subagent.model_copy(
        update={"pid": pid, "updated_at": now}
    )
    save_task_runtime(root, task)


def mark_subagent_progress(
    root: Path,
    task: TaskRecord,
    *,
    pid: int | None = None,
    transcript: str | None = None,
    continuation: RuntimeEngineContinuation | None = None,
) -> None:
    if task.runtime.active_subagent is None:
        return
    now = utcnow()
    task.runtime.updated_at = now
    if task.runtime.current_stage.step is not None:
        task.runtime.current_stage = task.runtime.current_stage.model_copy(
            update={"updated_at": now}
        )
    updates: dict[str, object] = {"updated_at": now}
    if pid is not None:
        updates["pid"] = pid
    if transcript is not None:
        updates["transcript_snippet"] = summarize_transcript(transcript)
    if continuation is not None:
        updates["continuation"] = continuation
    task.runtime.active_subagent = task.runtime.active_subagent.model_copy(update=updates)
    save_task_runtime(root, task)


def mark_subagent_finished(
    root: Path,
    task: TaskRecord,
    ref: SubagentRef,
    transcript: str,
    exit_code: int,
    pid: int | None = None,
    interruption_reason: str | None = None,
    resource_limit_event: ResourceLimitEvent | None = None,
    continuation: RuntimeEngineContinuation | None = None,
) -> None:
    now = utcnow()
    started_at = task.runtime.active_subagent.started_at if task.runtime.active_subagent else now
    runtime_pid = pid
    if runtime_pid is None and task.runtime.active_subagent is not None:
        runtime_pid = task.runtime.active_subagent.pid
    task.runtime.updated_at = now
    task.runtime.last_subagent = RuntimeSubagentState(
        id=ref.id,
        role=ref.role,
        engine=ref.engine,
        status=ref.status,
        path=ref.path,
        pid=runtime_pid,
        sandboxed=ref.sandboxed,
        sandbox_summary=ref.sandbox_summary,
        started_at=started_at,
        updated_at=now,
        completed_at=now,
        exit_code=exit_code,
        transcript_snippet=summarize_transcript(transcript),
        interruption_reason=interruption_reason or "",
        resource_limit_event=resource_limit_event,
        continuation=(
            continuation
            if continuation is not None
            else task.runtime.active_subagent.continuation
            if task.runtime.active_subagent is not None
            else None
        ),
    )
    task.runtime.active_subagent = None
    save_task_runtime(root, task)


def mark_engine_switch(
    root: Path,
    task: TaskRecord,
    *,
    step: str,
    from_engine: str,
    to_engine: str,
    reason: str,
) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.last_engine_switch = RuntimeEngineSwitch(
        step=step,
        from_engine=from_engine,
        to_engine=to_engine,
        reason=reason,
        happened_at=now,
    )
    save_task_runtime(root, task)


def summarize_transcript(transcript: str, limit: int = 120) -> str:
    for line in transcript.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("VERDICT:"):
            continue
        if stripped.startswith("SUMMARY:"):
            stripped = stripped.partition(":")[2].strip()
        return stripped if len(stripped) <= limit else stripped[: limit - 3].rstrip() + "..."
    return ""


def _duration_seconds(started_at: str | None, ended_at: str | None) -> int:
    if started_at is None or ended_at is None:
        return 0
    try:
        from datetime import datetime

        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))


def append_journal(root: Path, task: TaskRecord, message: str) -> None:
    journal = task_dir(root, task) / "journal.md"
    with workspace_mutation_guard(root):
        existing = journal.read_text(encoding="utf-8") if journal.exists() else ""
        _atomic_write_text(journal, f"{existing}\n## {utcnow()}\n{message}\n")


def set_active_task(root: Path, task_id: str | None) -> WorkspaceState:
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        state.active_task_id = task_id
        if task_id is not None and task_id in state.queue:
            state.queue = [item for item in state.queue if item != task_id]
        _validate_single_active_task(root, state)
        if task_id is None:
            save_state(root, state)
            return state
        task = require_task(root, task_id)
        if task.status == "queued":
            task.status = "in_progress"
        persist_task_and_state(root, task=task, state=state)
        return state


def peek_next_task(root: Path) -> TaskRecord | None:
    return peek_next_task_selection(root).task


def peek_next_task_selection(root: Path) -> TaskSelection:
    recover_stale_runner_state(root)
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        _validate_single_active_task(root, state)
        _reconcile_stale_runner_tasks(root, state)
        next_task, blocked, mutated = _resolve_next_task_from_state(root, state)
        if mutated:
            save_state(root, state)
        return TaskSelection(task=next_task, blocked=blocked)


def plan_task_selections(root: Path) -> TaskPlan:
    recover_stale_runner_state(root)
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        _validate_single_active_task(root, state)
        _reconcile_stale_runner_tasks(root, state)
        tasks_by_id = {task.id: task.model_copy(deep=True) for task in list_tasks(root)}
        policy = load_config(root).pool_selection_policy
        if policy not in VALID_POOL_SELECTION_POLICIES:
            policy = "dependency_aware"

        planned: list[TaskRecord] = []
        simulated_state = state.model_copy(deep=True)
        while True:
            next_task, blocked, _ = _resolve_next_task_from_snapshot(
                simulated_state,
                tasks_by_id,
                policy=policy,
            )
            if next_task is None:
                return TaskPlan(tasks=planned, blocked=blocked)

            planned.append(next_task.model_copy(deep=True))
            simulated_state.active_task_id = None
            simulated_state.queue = [item for item in simulated_state.queue if item != next_task.id]
            simulated_task = tasks_by_id[next_task.id]
            simulated_task.status = "done"
            simulated_task.pipeline_status = "done"


def dequeue_next_task(root: Path) -> TaskRecord | None:
    return dequeue_next_task_selection(root).task


def dequeue_next_task_selection(root: Path) -> TaskSelection:
    recover_stale_runner_state(root)
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        _validate_single_active_task(root, state)
        _reconcile_stale_runner_tasks(root, state)
        next_task, blocked, mutated = _resolve_next_task_from_state(root, state)
        if next_task is None:
            if mutated:
                save_state(root, state)
            return TaskSelection(task=None, blocked=blocked)
        if state.active_task_id != next_task.id:
            state.active_task_id = next_task.id
            state.queue = [item for item in state.queue if item != next_task.id]
            mutated = True
        if mutated:
            if next_task.status == "flagged":
                recovery_stage = _auto_recovery_stage_for_flagged_task(next_task)
                record_recovery_report(
                    root,
                    next_task,
                    trigger="flagged_task",
                    stage=next_task.pipeline_status,
                    summary=(
                        f"Recovered flagged task back to `{recovery_stage}` so it can run again."
                    ),
                    runnable_state="runnable",
                    failure_classification=next_task.runtime.last_outcome.reason_code,
                    actions=[
                        RecoveryAction(
                            action="requeue_stage",
                            summary=f"Reset task from flagged to queued/{recovery_stage}.",
                            metadata={
                                "from_stage": next_task.pipeline_status,
                                "to_stage": recovery_stage,
                            },
                        )
                    ],
                )
                _reset_task_for_recovery(
                    next_task,
                    status="queued",
                    pipeline_status=recovery_stage,
                    clear_last_outcome=False,
                )
            if next_task.status in {"queued", "interrupted"}:
                next_task.status = "in_progress"
            persist_task_and_state(root, task=next_task, state=state)
        return TaskSelection(task=next_task, blocked=blocked)


def _is_parked_task(task: TaskRecord) -> bool:
    return task.status == "parked"


def _is_task_eligible_for_execution(task: TaskRecord) -> bool:
    if task.pipeline_status == "done":
        return False
    if task.status in {"queued", "in_progress", "flagged"}:
        return True
    if task.status == "interrupted":
        return True
    return False


def _auto_recovery_stage_for_flagged_task(task: TaskRecord) -> str:
    if task.pipeline_status == "commit_to_git":
        return "commit_to_git"
    return implementation_entry_stage(task)


def _is_task_completed(task: TaskRecord) -> bool:
    return task.status == "done" and task.pipeline_status == "done"


def _task_blockers(task: TaskRecord, tasks_by_id: dict[str, TaskRecord]) -> list[str]:
    blockers: list[str] = []
    seen: set[str] = set()
    for dependency_id in task.depends_on:
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        dependency = tasks_by_id.get(dependency_id)
        if dependency is None:
            blockers.append(f"{dependency_id} (missing)")
            continue
        if not _is_task_completed(dependency):
            blockers.append(f"{dependency.id} ({dependency.status}/{dependency.pipeline_status})")
    return blockers


def _validate_task_dependencies(root: Path, *, task_id: str, depends_on: list[str]) -> None:
    tasks_by_id = {task.id: task for task in list_tasks(root)}
    seen: set[str] = set()
    for dependency_id in depends_on:
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        if dependency_id == task_id:
            raise ValueError(f"Task {task_id} cannot depend on itself")
        if dependency_id not in tasks_by_id:
            raise ValueError(f"Task {dependency_id} not found")
        if _dependency_reaches_task(task_id, dependency_id, tasks_by_id):
            raise ValueError(f"Task {task_id} dependency cycle detected via {dependency_id}")


def _dependency_reaches_task(
    task_id: str, dependency_id: str, tasks_by_id: dict[str, TaskRecord]
) -> bool:
    stack = [dependency_id]
    seen: set[str] = set()
    while stack:
        current_id = stack.pop()
        if current_id == task_id:
            return True
        if current_id in seen:
            continue
        seen.add(current_id)
        current = tasks_by_id.get(current_id)
        if current is None:
            continue
        stack.extend(current.depends_on)
    return False


def _dependent_task_count(
    task_id: str, queue: list[str], tasks_by_id: dict[str, TaskRecord]
) -> int:
    eligible_task_ids = {
        queued_id
        for queued_id in queue
        if (
            (queued_task := tasks_by_id.get(queued_id)) is not None
            and _is_task_eligible_for_execution(queued_task)
        )
    }
    reverse_dependencies: dict[str, set[str]] = {
        candidate_id: set() for candidate_id in eligible_task_ids
    }
    for queued_id in eligible_task_ids:
        queued_task = tasks_by_id[queued_id]
        for dependency_id in queued_task.depends_on:
            if dependency_id in reverse_dependencies:
                reverse_dependencies[dependency_id].add(queued_id)

    count = 0
    seen: set[str] = set()
    stack = list(reverse_dependencies.get(task_id, ()))
    while stack:
        dependent_id = stack.pop()
        if dependent_id in seen:
            continue
        seen.add(dependent_id)
        count += 1
        stack.extend(reverse_dependencies.get(dependent_id, ()))
    return count


def _is_interrupted_task(task: TaskRecord) -> bool:
    return _is_task_eligible_for_execution(task) and (
        task.status == "in_progress" or task.pipeline_status != "backlog"
    )


def _task_selection_key(
    task: TaskRecord,
    *,
    queue_index: int,
    queue: list[str],
    tasks_by_id: dict[str, TaskRecord],
    policy: str,
) -> tuple[int | str, ...]:
    interrupted_rank = 0 if _is_interrupted_task(task) else 1
    if policy == "fifo":
        return (interrupted_rank, queue_index, task.id)
    if policy == "priority_first":
        return (TASK_PRIORITY_ORDER.get(task.priority, 2), queue_index, interrupted_rank, task.id)
    if policy == "dependency_aware":
        return (
            queue_index,
            -_dependent_task_count(task.id, queue, tasks_by_id),
            interrupted_rank,
            task.id,
        )
    raise ValueError(f"Unsupported pool selection policy '{policy}'")


def _resolve_next_task_from_state(
    root: Path, state: WorkspaceState
) -> tuple[TaskRecord | None, list[BlockedTask], bool]:
    mutated = _recover_stranded_commit_tasks(root, state)
    tasks_by_id = {task.id: task for task in list_tasks(root)}
    policy = load_config(root).pool_selection_policy
    if policy not in VALID_POOL_SELECTION_POLICIES:
        policy = "dependency_aware"
    next_task, blocked, snapshot_mutated = _resolve_next_task_from_snapshot(
        state, tasks_by_id, policy=policy
    )
    return next_task, blocked, mutated or snapshot_mutated


def _is_stranded_commit_task(task: TaskRecord) -> bool:
    return (
        task.pipeline_status == "done"
        and task.git.commit_sha is None
        and task.git.checkpoint_attempts > 0
    )


def _is_orphaned_commit_stage_task(task: TaskRecord, state: WorkspaceState) -> bool:
    return (
        task.pipeline_status == "commit_to_git"
        and task.status in {"queued", "in_progress", "interrupted"}
        and state.active_task_id != task.id
        and task.id not in state.queue
    )


def _should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    return task.pipeline_status == "commit_to_git" and task.status in {
        "queued",
        "in_progress",
        "interrupted",
    }


def _prepare_recovered_commit_task(task: TaskRecord) -> None:
    now = utcnow()
    task.status = "queued"
    task.pipeline_status = "commit_to_git"
    task.runtime.execution_status = "idle"
    task.runtime.run_started_at = None
    task.runtime.active_subagent = None
    task.runtime.updated_at = now
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )


def _prepare_interrupted_task_for_requeue(task: TaskRecord) -> None:
    now = utcnow()
    task.status = "queued"
    task.runtime.execution_status = "idle"
    task.runtime.run_started_at = None
    task.runtime.active_subagent = None
    task.runtime.updated_at = now
    if task.runtime.current_stage.step is None:
        task.runtime.current_stage = task.runtime.current_stage.model_copy(
            update={
                "status": "idle",
                "started_at": None,
                "completed_at": None,
                "updated_at": now,
                "duration_seconds": 0,
                "verdict": None,
                "summary": "",
            }
        )
    else:
        task.runtime.current_stage = task.runtime.current_stage.model_copy(
            update={
                "status": "interrupted",
                "updated_at": now,
            }
        )


def _interrupted_subagent_snippet(
    root: Path, task: TaskRecord, active: RuntimeSubagentState
) -> str:
    subagent_base = task_dir(root, task) / active.path
    report_path = subagent_base / "report.yaml"
    if report_path.exists():
        try:
            report = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            report = {}
        if isinstance(report, dict):
            summary = str(report.get("summary") or "").strip()
            if summary:
                return summary
    transcript_path = subagent_base / "transcript.md"
    transcript_path = _resolve_artifact_path(subagent_base, "transcript.md")
    if transcript_path is not None:
        transcript = _read_text_artifact(transcript_path)
        snippet = summarize_transcript(transcript)
        if snippet:
            return snippet
    return active.transcript_snippet or "runner interrupted before subagent completion"


def _interrupted_subagent_reason(task: TaskRecord, reason: str) -> str:
    active = task.runtime.active_subagent
    last_subagent = task.runtime.last_subagent
    if (
        last_subagent is not None
        and last_subagent.interruption_reason
        and (active is None or last_subagent.id == active.id)
    ):
        return last_subagent.interruption_reason
    return reason


def _write_interrupted_subagent_artifacts(
    root: Path,
    task: TaskRecord,
    subagent: RuntimeSubagentState,
    *,
    resume_stage: str,
) -> None:
    now = utcnow()
    base = task_dir(root, task) / subagent.path
    session_path = base / "session.yaml"
    report_path = base / "report.yaml"
    writes: dict[Path, str] = {}

    session_payload: dict[str, object]
    if session_path.exists():
        existing_session = yaml.safe_load(session_path.read_text(encoding="utf-8")) or {}
        session_payload = existing_session if isinstance(existing_session, dict) else {}
    else:
        session_payload = {}
    session_payload.update(
        {
            "id": subagent.id,
            "role": subagent.role,
            "engine": subagent.engine,
            "status": subagent.status,
            "sandboxed": subagent.sandboxed,
            "sandbox": subagent.sandbox_summary or "host",
            "updated_at": now,
            "pid": subagent.pid,
            "exit_code": subagent.exit_code,
            "interruption_reason": subagent.interruption_reason or None,
            "resume_stage": resume_stage,
            "continuation": (
                None
                if subagent.continuation is None
                else subagent.continuation.model_dump(mode="python")
            ),
        }
    )
    writes[session_path] = yaml.safe_dump(session_payload, sort_keys=False)

    report_payload: dict[str, object]
    if report_path.exists():
        existing_report = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        report_payload = existing_report if isinstance(existing_report, dict) else {}
    else:
        report_payload = {}
    report_payload["status"] = subagent.status
    report_payload["summary"] = report_payload.get("summary") or subagent.transcript_snippet
    report_payload["interruption_reason"] = subagent.interruption_reason or None
    report_payload["resume_stage"] = resume_stage
    report_payload["continuation"] = (
        None if subagent.continuation is None else subagent.continuation.model_dump(mode="python")
    )
    writes[report_path] = yaml.safe_dump(report_payload, sort_keys=False)
    _write_atomic_files(writes)


def _mark_interrupted_subagent(
    root: Path, task: TaskRecord, *, reason: str, stage: str
) -> RuntimeSubagentState | None:
    active = task.runtime.active_subagent
    existing = task.runtime.last_subagent if task.runtime.last_subagent is not None else None
    if active is None and (existing is None or existing.status != "interrupted"):
        return None
    now = utcnow()
    source = active or existing
    assert source is not None
    if active is not None:
        for ref in reversed(task.subagents):
            if ref.id == active.id and ref.status == "running":
                ref.status = "interrupted"
                break
    snippet = (
        _interrupted_subagent_snippet(root, task, source)
        if active is not None or not source.transcript_snippet
        else source.transcript_snippet
    )
    interrupted = source.model_copy(
        update={
            "status": "interrupted",
            "updated_at": now,
            "completed_at": source.completed_at or now,
            "transcript_snippet": snippet,
            "interruption_reason": _interrupted_subagent_reason(task, reason),
        }
    )
    task.runtime.last_subagent = interrupted
    task.runtime.active_subagent = None
    _write_interrupted_subagent_artifacts(root, task, interrupted, resume_stage=stage)
    return interrupted


def _prepare_interrupted_task(
    root: Path,
    task: TaskRecord,
    *,
    stage: str,
    summary: str,
    reason: str | None = None,
) -> None:
    now = utcnow()
    run_started_at = task.runtime.run_started_at
    stage_started_at = task.runtime.current_stage.started_at
    started_at = task.runtime.current_stage.started_at or task.runtime.run_started_at
    interrupted_at = (
        task.runtime.active_subagent.updated_at
        if task.runtime.active_subagent is not None
        else task.runtime.current_stage.updated_at or started_at or now
    )
    task.status = "interrupted"
    task.pipeline_status = stage  # type: ignore[assignment]
    task.runtime.execution_status = "interrupted"
    task.runtime.run_started_at = None
    interruption_reason = reason or summary
    interrupted_subagent = _mark_interrupted_subagent(
        root, task, reason=interruption_reason, stage=stage
    )
    _apply_task_outcome(
        task,
        kind="interrupted",
        stage=stage,
        reason_code="execution_interrupted",
        reason=summary,
        retry_count=task.runtime.retry_count,
        retry_limit=task.runtime.retry_limit,
        retry_source=task.runtime.retry_source,
    )
    task.runtime.updated_at = now
    task.runtime.interruption = RuntimeInterruptionState(
        source="subagent" if interrupted_subagent is not None else "runner",
        stage=stage,
        pipeline_status=stage,
        resume_stage=stage,
        reason=interruption_reason,
        summary=summary,
        interrupted_at=interrupted_at,
        detected_at=now,
        run_started_at=run_started_at,
        stage_started_at=stage_started_at,
        subagent=interrupted_subagent,
    )
    task.runtime.continuation_handoff = RuntimeContinuationHandoff(
        step=stage,
        kind="restart",
        reason=interruption_reason,
        from_engine=None if interrupted_subagent is None else interrupted_subagent.engine,
        to_engine=None if interrupted_subagent is None else interrupted_subagent.engine,
        subagent_id=None if interrupted_subagent is None else interrupted_subagent.id,
        subagent_path=None if interrupted_subagent is None else interrupted_subagent.path,
        status=None if interrupted_subagent is None else interrupted_subagent.status,
        summary=summary,
        transcript_snippet=""
        if interrupted_subagent is None
        else interrupted_subagent.transcript_snippet,
        warnings=[],
        session_path=(
            None if interrupted_subagent is None else f"{interrupted_subagent.path}/session.yaml"
        ),
        report_path=(
            None if interrupted_subagent is None else f"{interrupted_subagent.path}/report.yaml"
        ),
        transcript_path=(
            None if interrupted_subagent is None else f"{interrupted_subagent.path}/transcript.md"
        ),
        continuation=(None if interrupted_subagent is None else interrupted_subagent.continuation),
        updated_at=now,
    )
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": stage,
            "status": "interrupted",
            "started_at": started_at,
            "completed_at": now,
            "updated_at": now,
            "duration_seconds": _duration_seconds(started_at, now),
            "verdict": "blocked",
            "summary": summary,
        }
    )


def interruption_journal_message(task: TaskRecord) -> str:
    interruption = task.runtime.interruption
    if interruption is None:
        return f"Interrupted run recorded. Resume from `{task.pipeline_status}`."
    parts = [
        (
            f"Interrupted {interruption.source} execution while `{interruption.stage or task.pipeline_status}` "
            f"was running."
        ),
        f"Reason: {interruption.reason or interruption.summary or 'unknown'}.",
    ]
    if interruption.subagent is not None:
        subagent = interruption.subagent
        pid = subagent.pid if subagent.pid is not None else "-"
        parts.append(
            (
                f"Subagent `{subagent.id}` ({subagent.role}/{subagent.engine}, pid={pid}, "
                f"path `{subagent.path}`) stopped with status `{subagent.status}`."
            )
        )
        if subagent.transcript_snippet:
            parts.append(f"Last snippet: {subagent.transcript_snippet}.")
    parts.append(f"Resume from `{interruption.resume_stage or task.pipeline_status}`.")
    return " ".join(parts)


def _stale_interruption_reason(task: TaskRecord, stage: str, *, stale_pid: bool = False) -> str:
    active = task.runtime.active_subagent
    if active is not None:
        pid_detail = ""
        if stale_pid and active.pid is not None:
            pid_detail = f", pid {active.pid} no longer alive"
        return (
            f"Stale runner detected while subagent `{active.id}` "
            f"({active.role}/{active.engine}{pid_detail}) was still marked running in `{stage}`."
        )
    return f"Stale runner detected while `{stage}` was still marked running."


def _recover_commit_task(root: Path, task: TaskRecord) -> str:
    summary = "Interrupted `commit_to_git` run recovered. Resume from `commit_to_git`."
    _prepare_interrupted_task(
        root,
        task,
        stage="commit_to_git",
        summary=summary,
        reason=_stale_interruption_reason(task, "commit_to_git"),
    )
    task.status = "queued"
    return interruption_journal_message(task)


def _latest_stage_report_verdict(root: Path, task: TaskRecord) -> str | None:
    reports_dir = task_dir(root, task) / "reports"
    if not reports_dir.exists():
        return None
    reports = sorted(reports_dir.glob("*.yaml"))
    if not reports:
        return None
    return _report_verdict(reports[-1])


def _latest_stage_report_verdict_for_step(root: Path, task: TaskRecord, step: str) -> str | None:
    reports_dir = task_dir(root, task) / "reports"
    if not reports_dir.exists():
        return None
    reports = sorted(reports_dir.glob(f"{step}-*.yaml"))
    if not reports:
        return None
    return _report_verdict(reports[-1])


def _report_verdict(path: Path) -> str | None:
    try:
        report = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    verdict = str(report.get("verdict") or "").strip().lower()
    return verdict or None


def _should_recover_flagged_commit_stage_task(root: Path, task: TaskRecord) -> bool:
    if task.pipeline_status != "commit_to_git" or task.status != "flagged":
        return False
    if task.git.commit_sha is not None:
        return False
    if task.git.merge_agent_attempts >= 1:
        return False
    if _latest_stage_report_verdict_for_step(root, task, "accepting") in {"pass", "accept"}:
        return True
    if _latest_stage_report_verdict(root, task) in {"pass", "accept"}:
        return True
    return _latest_stage_report_verdict_for_step(root, task, "testing") in {"pass", "accept"}


def _should_resume_done_task_at_commit_stage(root: Path, task: TaskRecord) -> bool:
    if task.status != "done" or task.pipeline_status != "done":
        return False
    if task.git.commit_sha is not None:
        return False
    config = load_config(root)
    if not config.auto_commit or not task.git.auto_commit:
        return False
    return _latest_stage_report_verdict_for_step(root, task, "accepting") in {"pass", "accept"}


def _recover_flagged_commit_task(task: TaskRecord) -> str:
    _prepare_recovered_commit_task(task)
    return "Recovered flagged accepted task back to `queued/commit_to_git` for final checkpoint commit."


def _finalize_recovered_commit_task(task: TaskRecord, *, commit_sha: str) -> str:
    now = utcnow()
    started_at = task.runtime.current_stage.started_at
    task.status = "done"
    task.pipeline_status = "done"
    set_task_commit_sha(task, commit_sha)
    task.runtime.execution_status = "done"
    task.runtime.run_started_at = None
    task.runtime.active_subagent = None
    task.runtime.updated_at = now
    task.runtime.last_stage = task.runtime.last_stage.model_copy(
        update={
            "step": "commit_to_git",
            "status": "completed",
            "started_at": started_at,
            "completed_at": now,
            "updated_at": now,
            "duration_seconds": _duration_seconds(started_at, now),
            "verdict": "pass",
            "summary": "Recovered existing checkpoint commit after interrupted `commit_to_git`.",
        }
    )
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    return (
        "Recovered existing checkpoint commit after interrupted `commit_to_git` "
        f"and finalized the task at `{commit_sha}`."
    )


def _find_existing_checkpoint_commit(root: Path, task: TaskRecord) -> str | None:
    if not is_git_repo(root):
        return None
    try:
        return find_commit_by_subject(
            root,
            checkpoint_message(task, attempt=task.git.checkpoint_attempts),
        )
    except GitError:
        return None


def _recover_existing_checkpoint_commit(root: Path, task: TaskRecord) -> str | None:
    commit_sha = _find_existing_checkpoint_commit(root, task)
    if commit_sha is None:
        return None
    return _finalize_recovered_commit_task(task, commit_sha=commit_sha)


def _recover_stranded_commit_tasks(root: Path, state: WorkspaceState) -> bool:
    tasks = list_tasks(root)
    stranded = [task for task in tasks if _is_stranded_commit_task(task)]
    accepted_without_commit = {
        task.id: task for task in tasks if _should_resume_done_task_at_commit_stage(root, task)
    }
    orphaned = [task for task in tasks if _is_orphaned_commit_stage_task(task, state)]
    flagged_commit_ready = {
        task.id: task for task in tasks if _should_recover_flagged_commit_stage_task(root, task)
    }
    completed_ids: set[str] = set()
    recovered: list[TaskRecord] = []
    transitioned: list[TaskRecord] = []
    journal_messages: dict[str, str] = {}
    for task in stranded:
        journal_message = _recover_existing_checkpoint_commit(root, task)
        if journal_message is not None:
            completed_ids.add(task.id)
            transitioned.append(task)
            journal_messages[task.id] = journal_message
            continue
        recovered.append(task)
    recovered.extend(
        task for task_id, task in accepted_without_commit.items() if task_id not in completed_ids
    )
    recovered.extend(orphaned)
    recovered.extend(flagged_commit_ready.values())
    recovered_ids = {task.id for task in recovered}
    resolved_ids = {*recovered_ids, *completed_ids}
    queue = [task_id for task_id in state.queue if task_id not in recovered_ids]
    for task in recovered:
        if task.id in accepted_without_commit:
            journal_messages[task.id] = (
                "Recovered accepted task back to `queued/commit_to_git` "
                "because no final checkpoint commit was recorded."
            )
            _prepare_recovered_commit_task(task)
        elif task.id in flagged_commit_ready:
            journal_messages[task.id] = _recover_flagged_commit_task(task)
        else:
            journal_messages[task.id] = _recover_commit_task(root, task)
        transitioned.append(task)
    queue = [task_id for task_id in queue if task_id not in completed_ids]
    if recovered:
        queue = [*(task.id for task in recovered), *queue]
    if state.active_task_id in resolved_ids:
        state.active_task_id = None
    state.queue = queue
    if not transitioned:
        return False
    _persist_tasks_and_state_without_runner_guard(
        root,
        tasks=transitioned,
        state=state,
        journal_messages=journal_messages,
    )
    return True


def _has_inactive_running_tasks(
    root: Path,
    tasks_by_id: dict[str, TaskRecord],
    timeout_seconds: float,
) -> bool:
    """Check whether any running task has been inactive based on last event timestamp."""
    from datetime import UTC, datetime

    from litehive.events import last_event_timestamp

    for task_id, task in tasks_by_id.items():
        if task.runtime.execution_status != "running":
            continue
        ts_str = last_event_timestamp(root, task)
        if ts_str is None:
            continue
        try:
            event_time = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        elapsed = (now - event_time).total_seconds()
        if elapsed > timeout_seconds:
            return True
    return False


def _recover_stale_runner_state(
    root: Path,
    *,
    summary: WorkspaceRepairSummary | None = None,
) -> bool:
    root = root.resolve()
    with _workspace_lock(root):
        state = load_state(root)
        tasks = list_tasks(root)
        tasks_by_id = {task.id: task for task in tasks}
        running_task_ids = sorted(
            task.id for task in tasks if task.runtime.execution_status == "running"
        )
        if not _current_thread_owns_runner_guard(root) and _runner_lock_is_held(root):
            if not _runner_lock_pid_is_stale(root):
                config = load_config(root)
                if config.inactivity_timeout_seconds is None:
                    return False
                if not _has_inactive_running_tasks(
                    root, tasks_by_id, config.inactivity_timeout_seconds
                ):
                    return False

        runner_metadata = _read_runner_lock_metadata(root)
        has_stale_metadata = _runner_metadata_present(runner_metadata)

        mutated = False
        transitioned: list[TaskRecord] = []
        journal_messages: dict[str, str] = {}
        prioritized_ids: list[str] = []

        if len(running_task_ids) > 1:
            return False

        for task_id in running_task_ids:
            task = tasks_by_id.get(task_id)
            if task is None:
                continue
            if (
                task_id != state.active_task_id
                and not _is_stranded_commit_task(task)
                and not _should_requeue_commit_stage_task(task)
                and not has_stale_metadata
            ):
                continue
            if _is_stranded_commit_task(task):
                continue
            stale_pid = _subagent_process_is_stale(task)
            if _should_requeue_commit_stage_task(task):
                journal_message = _recover_existing_checkpoint_commit(root, task)
                if journal_message is not None:
                    journal_messages[task.id] = journal_message
                    record_recovery_report(
                        root,
                        task,
                        trigger="stale_runner_recovery",
                        stage="commit_to_git",
                        summary="Recovered existing checkpoint commit during stale runner recovery.",
                        runnable_state="runnable",
                        failure_classification="stale_runner",
                        actions=[
                            RecoveryAction(
                                action="clear_stale_active_state",
                                summary="Cleared stale active runner state for the task.",
                            ),
                            RecoveryAction(
                                action="finalize_existing_checkpoint",
                                summary="Recorded the existing checkpoint commit and finalized the task.",
                                metadata={"commit_sha": task.git.commit_sha},
                            ),
                        ],
                        warnings=["stale subagent pid detected"] if stale_pid else [],
                    )
                    if summary is not None and task.id not in summary.finalized_commit_task_ids:
                        summary.finalized_commit_task_ids.append(task.id)
                    if (
                        stale_pid
                        and summary is not None
                        and task.id not in summary.stale_process_task_ids
                    ):
                        summary.stale_process_task_ids.append(task.id)
                else:
                    journal_messages[task.id] = _recover_commit_task(root, task)
                    record_recovery_report(
                        root,
                        task,
                        trigger="stale_runner_recovery",
                        stage="commit_to_git",
                        summary="Recovered stale runner state and requeued commit_to_git.",
                        runnable_state="runnable",
                        failure_classification="stale_runner",
                        actions=[
                            RecoveryAction(
                                action="clear_stale_active_state",
                                summary="Cleared stale active runner state for the task.",
                            ),
                            RecoveryAction(
                                action="requeue_stage",
                                summary="Requeued the task at commit_to_git.",
                                metadata={"stage": "commit_to_git"},
                            ),
                        ],
                        warnings=["stale subagent pid detected"] if stale_pid else [],
                    )
                    if summary is not None and task.id not in summary.requeued_task_ids:
                        summary.requeued_task_ids.append(task.id)
                    prioritized_ids.append(task.id)
                if (
                    stale_pid
                    and summary is not None
                    and task.id not in summary.stale_process_task_ids
                ):
                    summary.stale_process_task_ids.append(task.id)
            elif _is_task_eligible_for_execution(task):
                _prepare_interrupted_task(
                    root,
                    task,
                    stage=task.pipeline_status,
                    summary=(
                        "Interrupted run recovered after stale runner detection. "
                        f"Resume from `{task.pipeline_status}`."
                    ),
                    reason=_stale_interruption_reason(
                        task, task.pipeline_status, stale_pid=stale_pid
                    ),
                )
                journal_messages[task.id] = interruption_journal_message(task)
                record_recovery_report(
                    root,
                    task,
                    trigger="stale_runner_recovery",
                    stage=task.pipeline_status,
                    summary=(
                        f"Recovered stale runner state and returned the task to `{task.pipeline_status}`."
                    ),
                    runnable_state="runnable",
                    failure_classification="stale_runner",
                    actions=[
                        RecoveryAction(
                            action="clear_stale_active_state",
                            summary="Cleared stale active runner state for the task.",
                        ),
                        RecoveryAction(
                            action="requeue_stage",
                            summary=f"Requeued the task at {task.pipeline_status}.",
                            metadata={"stage": task.pipeline_status},
                        ),
                    ],
                    warnings=["stale subagent pid detected"] if stale_pid else [],
                )
                if summary is not None and task.id not in summary.requeued_task_ids:
                    summary.requeued_task_ids.append(task.id)
                prioritized_ids.append(task.id)
                if (
                    stale_pid
                    and summary is not None
                    and task.id not in summary.stale_process_task_ids
                ):
                    summary.stale_process_task_ids.append(task.id)
            else:
                continue
            transitioned.append(task)
            mutated = True

        if transitioned:
            if state.active_task_id is not None:
                if summary is not None and summary.cleared_active_task_id is None:
                    summary.cleared_active_task_id = state.active_task_id
                state.active_task_id = None
            state.queue = [task_id for task_id in state.queue if task_id not in running_task_ids]
            requeued_ids = list(prioritized_ids)
            if requeued_ids:
                state.queue = [*requeued_ids, *state.queue]

        if state.active_task_id is not None and (
            state.active_task_id not in tasks_by_id or state.active_task_id in prioritized_ids
        ):
            if summary is not None and summary.cleared_active_task_id is None:
                summary.cleared_active_task_id = state.active_task_id
            state.active_task_id = None
            mutated = True

        commit_mutated = _recover_stranded_commit_tasks(root, state)
        if summary is not None and commit_mutated:
            refreshed_tasks = {task.id: task for task in list_tasks(root)}
            finalized_ids = [
                task_id
                for task_id, task in refreshed_tasks.items()
                if _is_stranded_commit_task(tasks_by_id.get(task_id, task))
                and task.runtime.execution_status == "done"
            ]
            for task_id in sorted(finalized_ids):
                if task_id not in summary.finalized_commit_task_ids:
                    summary.finalized_commit_task_ids.append(task_id)
            for task_id, task in refreshed_tasks.items():
                previous = tasks_by_id.get(task_id)
                if previous is None:
                    continue
                if (
                    _should_recover_flagged_commit_stage_task(root, previous)
                    and task.pipeline_status == "commit_to_git"
                    and task.status == "queued"
                    and task.id not in summary.requeued_task_ids
                ):
                    summary.requeued_task_ids.append(task.id)
                    continue
                if (
                    _is_stranded_commit_task(previous)
                    and not _is_stranded_commit_task(task)
                    and task.pipeline_status == "commit_to_git"
                    and task.id not in summary.requeued_task_ids
                ):
                    summary.requeued_task_ids.append(task.id)
        if transitioned:
            _persist_tasks_and_state_without_runner_guard(
                root,
                tasks=transitioned,
                state=state,
                journal_messages=journal_messages,
            )
        elif mutated:
            _save_state_without_runner_guard(root, state)
        return mutated or commit_mutated


def recover_stale_runner_state(root: Path) -> bool:
    return _recover_stale_runner_state(root)


def repair_workspace_state(root: Path) -> WorkspaceRepairSummary:
    summary = WorkspaceRepairSummary()
    summary.stale_runner_recovered = _recover_stale_runner_state(root, summary=summary)
    summary.mutated = summary.stale_runner_recovered

    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        tasks_by_id = {task.id: task for task in list_tasks(root)}
        touched_tasks: list[TaskRecord] = []
        journal_messages: dict[str, str] = {}
        recovered_flagged_commit_ids: list[str] = []

        if state.active_task_id is not None and state.active_task_id not in tasks_by_id:
            summary.cleared_active_task_id = state.active_task_id
            state.active_task_id = None
            summary.mutated = True

        active_task = (
            tasks_by_id.get(state.active_task_id) if state.active_task_id is not None else None
        )
        if active_task is not None and active_task.runtime.execution_status != "running":
            state.active_task_id = None
            summary.cleared_active_task_id = active_task.id
            summary.mutated = True
            if _is_stranded_commit_task(active_task):
                journal_message = _recover_existing_checkpoint_commit(root, active_task)
                if journal_message is None:
                    journal_message = _recover_commit_task(root, active_task)
                    _enqueue_recovered_task(state, active_task.id)
                    if active_task.id not in summary.requeued_task_ids:
                        summary.requeued_task_ids.append(active_task.id)
                else:
                    state.queue = [item for item in state.queue if item != active_task.id]
                    if active_task.id not in summary.finalized_commit_task_ids:
                        summary.finalized_commit_task_ids.append(active_task.id)
                journal_messages[active_task.id] = journal_message
                touched_tasks.append(active_task)
            elif _should_requeue_commit_stage_task(active_task):
                _prepare_recovered_commit_task(active_task)
                _enqueue_recovered_task(state, active_task.id)
                journal_messages[active_task.id] = (
                    "Recovered interrupted `commit_to_git` attempt and requeued the task at "
                    "`commit_to_git`."
                )
                touched_tasks.append(active_task)
                if active_task.id not in summary.requeued_task_ids:
                    summary.requeued_task_ids.append(active_task.id)
            elif _is_task_eligible_for_execution(active_task):
                _prepare_interrupted_task_for_requeue(active_task)
                _enqueue_recovered_task(state, active_task.id)
                journal_messages[active_task.id] = (
                    "Recovered interrupted run and requeued the task at "
                    f"`{active_task.pipeline_status}`."
                )
                touched_tasks.append(active_task)
                if active_task.id not in summary.requeued_task_ids:
                    summary.requeued_task_ids.append(active_task.id)

        for task in tasks_by_id.values():
            if not _should_recover_flagged_commit_stage_task(root, task):
                continue
            journal_messages[task.id] = _recover_flagged_commit_task(task)
            touched_tasks.append(task)
            recovered_flagged_commit_ids.append(task.id)
            if task.id not in summary.requeued_task_ids:
                summary.requeued_task_ids.append(task.id)
            summary.mutated = True

        if recovered_flagged_commit_ids:
            state.queue = [
                task_id for task_id in state.queue if task_id not in recovered_flagged_commit_ids
            ]
            state.queue = [*recovered_flagged_commit_ids, *state.queue]

        seen: set[str] = set()
        normalized_queue: list[str] = []
        for task_id in state.queue:
            task = tasks_by_id.get(task_id)
            if task is None or not _is_task_eligible_for_execution(task):
                summary.removed_queue_entries.append(task_id)
                summary.mutated = True
                continue
            if task_id in seen:
                summary.deduped_queue_entries.append(task_id)
                summary.mutated = True
                continue
            seen.add(task_id)
            normalized_queue.append(task_id)
        state.queue = normalized_queue

        restored = _restore_missing_queued_tasks(state, tasks_by_id)
        if restored:
            summary.restored_queue_entries.extend(restored)
            summary.mutated = True

        if touched_tasks:
            _persist_tasks_and_state_without_runner_guard(
                root,
                tasks=touched_tasks,
                state=state,
                journal_messages=journal_messages,
            )
        elif summary.mutated:
            _save_state_without_runner_guard(root, state)
    return summary


def _restore_missing_queued_tasks(
    state: WorkspaceState,
    tasks_by_id: dict[str, TaskRecord],
) -> list[str]:
    restored: list[str] = []
    queued_ids = set(state.queue)
    for task_id, task in tasks_by_id.items():
        if task.status not in {"queued", "interrupted", "flagged"}:
            continue
        if task.pipeline_status == "done":
            continue
        if _is_parked_task(task):
            continue
        if task_id == state.active_task_id or task_id in queued_ids:
            continue
        state.queue.append(task_id)
        queued_ids.add(task_id)
        restored.append(task_id)
    return restored


def _reconcile_stale_runner_tasks(root: Path, state: WorkspaceState) -> bool:
    tasks = list_tasks(root)
    tasks_by_id = {task.id: task for task in tasks}
    transitioned: list[TaskRecord] = []
    journal_messages: dict[str, str] = {}
    queue = list(state.queue)
    active_task = (
        tasks_by_id.get(state.active_task_id) if state.active_task_id is not None else None
    )

    if state.active_task_id is not None and active_task is None:
        state.active_task_id = None

    running_tasks = [t for t in tasks if t.runtime.execution_status == "running"]
    if len(running_tasks) > 1:
        return False

    for task in tasks:
        if task.runtime.execution_status != "running":
            continue
        if task.id == state.active_task_id:
            continue
        if _is_stranded_commit_task(task) or _should_requeue_commit_stage_task(task):
            queue = [item for item in queue if item != task.id]
            journal_messages[task.id] = _recover_existing_checkpoint_commit(
                root, task
            ) or _recover_commit_task(root, task)
            if task.status == "queued":
                queue.insert(0, task.id)
            transitioned.append(task)
            continue
        if _is_task_eligible_for_execution(task):
            _prepare_interrupted_task_for_requeue(task)
            queue = [item for item in queue if item != task.id]
            queue.insert(0, task.id)
            journal_messages[task.id] = (
                f"Reconciled stale runner state and requeued the task at `{task.pipeline_status}`."
            )
            transitioned.append(task)

    # Also reconcile the active task if runner is not live
    if state.active_task_id is not None:
        active_task = tasks_by_id.get(state.active_task_id)
        if (
            active_task is not None
            and active_task.runtime.execution_status == "running"
            and not _runner_lock_is_held(root)
        ):
            if _is_stranded_commit_task(active_task) or _should_requeue_commit_stage_task(
                active_task
            ):
                queue = [item for item in queue if item != active_task.id]
                journal_messages[active_task.id] = _recover_existing_checkpoint_commit(
                    root, active_task
                ) or _recover_commit_task(root, active_task)
                if active_task.status == "queued":
                    queue.insert(0, active_task.id)
                transitioned.append(active_task)
            elif _is_task_eligible_for_execution(active_task):
                _prepare_interrupted_task_for_requeue(active_task)
                queue = [item for item in queue if item != active_task.id]
                queue.insert(0, active_task.id)
                journal_messages[active_task.id] = (
                    f"Reconciled stale active task and requeued at `{active_task.pipeline_status}`."
                )
                transitioned.append(active_task)

            if active_task in transitioned:
                state.active_task_id = None

    if transitioned:
        state.queue = queue
        _persist_tasks_and_state_without_runner_guard(
            root,
            tasks=transitioned,
            state=state,
            journal_messages=journal_messages,
        )

    return bool(transitioned)


def _resolve_next_task_from_snapshot(
    state: WorkspaceState,
    tasks_by_id: dict[str, TaskRecord],
    *,
    policy: str,
) -> tuple[TaskRecord | None, list[BlockedTask], bool]:
    mutated = False
    blocked: list[BlockedTask] = []
    blocked_task_ids: set[str] = set()
    if _restore_missing_queued_tasks(state, tasks_by_id):
        mutated = True
    if state.active_task_id is not None:
        active_task = tasks_by_id.get(state.active_task_id)
        if active_task is not None and _is_task_eligible_for_execution(active_task):
            blockers = _task_blockers(active_task, tasks_by_id)
            if not blockers:
                return active_task, blocked, mutated
            if active_task.id not in state.queue:
                state.queue.insert(0, active_task.id)
            blocked.append(
                BlockedTask(
                    task_id=active_task.id,
                    title=active_task.title,
                    queue_position=1,
                    blocked_by=blockers,
                )
            )
            blocked_task_ids.add(active_task.id)
        state.active_task_id = None
        mutated = True

    ready_candidates: list[tuple[tuple[int, int, str], TaskRecord]] = []
    for index, next_id in enumerate(list(state.queue), start=1):
        next_task = tasks_by_id.get(next_id)
        if next_task is None or not _is_task_eligible_for_execution(next_task):
            state.queue.remove(next_id)
            mutated = True
            continue
        blockers = _task_blockers(next_task, tasks_by_id)
        if blockers:
            if next_task.id not in blocked_task_ids:
                blocked.append(
                    BlockedTask(
                        task_id=next_task.id,
                        title=next_task.title,
                        queue_position=index,
                        blocked_by=blockers,
                    )
                )
                blocked_task_ids.add(next_task.id)
            continue
        ready_candidates.append(
            (
                _task_selection_key(
                    next_task,
                    queue_index=index,
                    queue=list(state.queue),
                    tasks_by_id=tasks_by_id,
                    policy=policy,
                ),
                next_task,
            )
        )

    if ready_candidates:
        ready_candidates.sort(key=lambda item: item[0])
        return ready_candidates[0][1], blocked, mutated

    return None, blocked, mutated


def clear_active_task(root: Path) -> WorkspaceState:
    return set_active_task(root, None)


def restore_untouched_active_task(root: Path) -> WorkspaceState:
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        _validate_single_active_task(root, state)
        _reconcile_stale_runner_tasks(root, state)
        if state.active_task_id is None:
            return state

        task = get_task(root, state.active_task_id)
        if task is not None and _is_stranded_commit_task(task):
            journal_message = _recover_existing_checkpoint_commit(root, task)
            state.active_task_id = None
            state.queue = [item for item in state.queue if item != task.id]
            if journal_message is None:
                _prepare_interrupted_task(
                    root,
                    task,
                    stage="commit_to_git",
                    summary="Interrupted `commit_to_git` run recovered. Resume from `commit_to_git`.",
                    reason=_stale_interruption_reason(task, "commit_to_git"),
                )
                task.status = "queued"
                _enqueue_recovered_task(state, task.id)
                persist_task_and_state(
                    root,
                    task=task,
                    state=state,
                    journal_message=interruption_journal_message(task),
                )
                return state

            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=journal_message,
            )
            return state

        if task is not None and _should_requeue_commit_stage_task(task):
            journal_message = _recover_existing_checkpoint_commit(root, task)
            if journal_message is not None:
                state.active_task_id = None
                state.queue = [item for item in state.queue if item != task.id]
                persist_task_and_state(
                    root,
                    task=task,
                    state=state,
                    journal_message=journal_message,
                )
                return state
            _prepare_interrupted_task(
                root,
                task,
                stage="commit_to_git",
                summary="Interrupted `commit_to_git` run recovered. Resume from `commit_to_git`.",
                reason=_stale_interruption_reason(task, "commit_to_git"),
            )
            task.status = "queued"
            _enqueue_recovered_task(state, task.id)
            state.active_task_id = None
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=interruption_journal_message(task),
            )
            return state

        if (
            task is not None
            and _is_task_eligible_for_execution(task)
            and task.runtime.execution_status != "running"
        ):
            task.status = "queued"
            _enqueue_recovered_task(state, task.id)
            state.active_task_id = None
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=f"Restored untouched active task to queue at `{task.pipeline_status}`.",
            )
            return state

        if task is not None and _is_task_eligible_for_execution(task):
            _prepare_interrupted_task(
                root,
                task,
                stage=task.pipeline_status,
                summary=f"Interrupted run recovered. Resume from `{task.pipeline_status}`.",
                reason=_stale_interruption_reason(task, task.pipeline_status),
            )
            if not _is_parked_task(task):
                task.status = "queued"
                _enqueue_recovered_task(state, task.id)
            state.active_task_id = None
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=interruption_journal_message(task),
            )
            return state

        state.active_task_id = None
        save_state(root, state)
        return state


def _active_task_id_for_stop(root: Path, state: WorkspaceState) -> str:
    markers = active_task_markers(root, state)
    if not markers:
        raise ValueError("No active task to stop")
    if len(markers) > 1:
        _validate_single_active_task(root, state)
    return next(iter(sorted(markers)))


def _stop_active_task_without_runner_guard(root: Path, task_id: str) -> TaskRecord:
    with _workspace_lock(root):
        state = load_state(root)
        active_task_id = _active_task_id_for_stop(root, state)
        if active_task_id != task_id:
            raise WorkspaceConflictError(
                f"task {task_id} is no longer the active task in this workspace"
            )
        task = require_task(root, task_id)
        if task.pipeline_status == "done":
            raise ValueError(f"Task {task.id} is already done")
        stage = task.runtime.current_stage.step or task.pipeline_status
        _prepare_interrupted_task(
            root,
            task,
            stage=stage,
            summary=f"Execution interrupted via `litehive stop`. Resume from `{stage}`.",
            reason="Task stopped via CLI",
        )
        task.status = "parked"
        if stage == "commit_to_git":
            task.status = "queued"
        state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        if task.status == "queued" and task.pipeline_status != "done":
            state.queue.insert(0, task.id)
        _persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=interruption_journal_message(task),
        )
        return task


def stop_current_task(
    root: Path,
    *,
    wait_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.1,
) -> StopTaskSummary:
    state = load_state(root)
    active_task_id = _active_task_id_for_stop(root, state)
    runner_pid: int | None = None
    if _runner_lock_is_held(root):
        metadata = _read_runner_lock_metadata(root)
        pid = metadata.pid
        if _runner_pid_is_alive(pid):
            runner_pid = int(pid)
            os.kill(runner_pid, signal.SIGINT)
            deadline = time.monotonic() + max(wait_timeout_seconds, 0.0)
            sleep_interval = max(poll_interval_seconds, 0.01)
            while _runner_lock_is_held(root) and time.monotonic() < deadline:
                time.sleep(sleep_interval)
            if _runner_lock_is_held(root):
                raise WorkspaceConflictError(
                    f"runner for task {active_task_id} did not stop cleanly after SIGINT (pid={runner_pid})"
                )
            recover_stale_runner_state(root)
            state = load_state(root)
            markers = active_task_markers(root, state)
            if active_task_id not in markers:
                return StopTaskSummary(
                    task=require_task(root, active_task_id),
                    runner_pid=runner_pid,
                    signal_sent=True,
                )
        else:
            raise WorkspaceConflictError(
                f"runner for task {active_task_id} is active but has no live PID to signal cleanly"
            )

    task = _stop_active_task_without_runner_guard(root, active_task_id)
    return StopTaskSummary(task=task, runner_pid=runner_pid, signal_sent=runner_pid is not None)


def _effective_task_engine(root: Path, task: TaskRecord) -> str:
    if task.runtime.active_subagent is not None:
        return task.runtime.active_subagent.engine
    if task.runtime.last_subagent is not None:
        return task.runtime.last_subagent.engine
    return task.engine or load_config(root).default_engine


def _switch_prior_work_paths(root: Path, task: TaskRecord) -> list[str]:
    paths: list[str] = []
    handoff = task.runtime.continuation_handoff
    for candidate in (
        None if handoff is None else handoff.subagent_path,
        None if handoff is None else handoff.transcript_path,
        None if handoff is None else handoff.report_path,
        None if handoff is None else handoff.session_path,
        None if task.runtime.last_subagent is None else task.runtime.last_subagent.path,
    ):
        if candidate and candidate not in paths:
            paths.append(candidate)
    base = _latest_subagent_base(root, task)
    if base is not None:
        rel_path = str(base.relative_to(task_dir(root, task)))
        if rel_path not in paths:
            paths.append(rel_path)
    return paths


def _switch_thread_comment_message(
    task: TaskRecord,
    *,
    reason: str,
    previous_engine: str,
    new_engine: str,
    prior_work_paths: list[str],
) -> str:
    lines = [
        f"Engine switch requested: {reason}",
        f"engine: {previous_engine} -> {new_engine}",
        f"resume_from: {task.pipeline_status}",
    ]
    if prior_work_paths:
        lines.append("prior_work:")
        lines.extend(f"- {path}" for path in prior_work_paths)
    else:
        lines.append("prior_work: no prior subagent artifacts recorded")
    return "\n".join(lines)


def switch_task_engine(root: Path, task_id: str, *, engine: str, reason: str) -> SwitchTaskSummary:
    if engine not in VALID_TASK_ENGINES:
        raise ValueError(f"Unsupported engine '{engine}'")
    if not reason.strip():
        raise ValueError("Switch reason must not be empty")

    task = require_task(root, task_id)
    if task.pipeline_status == "done":
        raise ValueError(f"Task {task.id} is already done")
    if task.pipeline_status == "backlog":
        raise ValueError(f"Task {task.id} is still in backlog and has no runnable stage to resume")

    state = load_state(root)
    was_active = state.active_task_id == task_id
    runner_pid: int | None = None
    signal_sent = False
    if was_active:
        stop_summary = stop_current_task(root)
        task = stop_summary.task
        runner_pid = stop_summary.runner_pid
        signal_sent = stop_summary.signal_sent
    else:
        task = require_task(root, task_id)

    previous_engine = _effective_task_engine(root, task)
    update_task(
        root,
        task.id,
        engine=engine,
    )
    task = require_task(root, task.id)
    mark_engine_switch(
        root,
        task,
        step=task.pipeline_status,
        from_engine=previous_engine,
        to_engine=engine,
        reason=reason.strip(),
    )
    task = require_task(root, task.id)

    if task.status == "queued":
        move_queued_task(root, task.id, 1)
        task = require_task(root, task.id)
    elif task.status in {"interrupted", "parked", "flagged", *CLOSED_TASK_STATUSES}:
        task = resume_task(root, task.id, front=True)
    else:
        raise ValueError(
            f"Task {task.id} is {task.status} and cannot be switched into a queued runnable state"
        )

    prior_work_paths = _switch_prior_work_paths(root, task)
    from litehive.models import TaskThreadComment

    append_thread_comment(
        root,
        task,
        TaskThreadComment(
            role="operator",
            step=task.pipeline_status,
            verdict="comment",
            message=_switch_thread_comment_message(
                task,
                reason=reason.strip(),
                previous_engine=previous_engine,
                new_engine=engine,
                prior_work_paths=prior_work_paths,
            ),
        ),
    )
    return SwitchTaskSummary(
        task=task,
        previous_engine=previous_engine,
        new_engine=engine,
        was_active=was_active,
        runner_pid=runner_pid,
        signal_sent=signal_sent,
        prior_work_paths=prior_work_paths,
    )


def enqueue_task(root: Path, task_id: str) -> WorkspaceState:
    return _enqueue_task(root, task_id, front=False)


def enqueue_task_front(root: Path, task_id: str) -> WorkspaceState:
    return _enqueue_task(root, task_id, front=True)


def _enqueue_task(root: Path, task_id: str, *, front: bool) -> WorkspaceState:
    with _workspace_lock(root):
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, [task_id], state=state)
        state.queue = [item for item in state.queue if item != task_id]
        if front:
            state.queue.insert(0, task_id)
        else:
            state.queue.append(task_id)
        _save_state_without_runner_guard(root, state)
        return state


def move_queued_task(root: Path, task_id: str, position: int) -> WorkspaceState:
    if position < 1:
        raise ValueError("Queue position must be 1 or greater")
    with _workspace_lock(root):
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, [task_id], state=state)
        if task_id not in state.queue:
            raise ValueError(f"Task {task_id} is not queued")
        queue = [item for item in state.queue if item != task_id]
        target_index = min(position - 1, len(queue))
        queue.insert(target_index, task_id)
        state.queue = queue
        _save_state_without_runner_guard(root, state)
        return state


def prioritize_queued_tasks(root: Path, task_ids: list[str]) -> WorkspaceState:
    if not task_ids:
        raise ValueError("At least one task id is required")
    seen: set[str] = set()
    duplicates: set[str] = set()
    for task_id in task_ids:
        if task_id in seen:
            duplicates.add(task_id)
            continue
        seen.add(task_id)
    if duplicates:
        joined = ", ".join(sorted(duplicates))
        raise ValueError(f"Task ids must be unique: {joined}")
    with _workspace_lock(root):
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, task_ids, state=state)
        missing = [task_id for task_id in task_ids if task_id not in state.queue]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"Tasks are not queued: {joined}")
        remaining = [queued_id for queued_id in state.queue if queued_id not in task_ids]
        state.queue = [*task_ids, *remaining]
        _save_state_without_runner_guard(root, state)
        return state


def _reset_task_for_recovery(
    task: TaskRecord,
    *,
    status: str,
    pipeline_status: str,
    clear_last_outcome: bool = True,
    preserve_continuation_handoff: bool = False,
) -> None:
    now = utcnow()
    task.status = status
    task.pipeline_status = pipeline_status
    task.runtime.execution_status = "idle"
    task.runtime.run_started_at = None
    task.runtime.updated_at = now
    task.runtime.active_subagent = None
    task.runtime.interruption = None
    task.runtime.retry_count = 0
    task.runtime.retry_limit = 0
    task.runtime.retry_source = "global"
    if not preserve_continuation_handoff:
        task.runtime.continuation_handoff = None
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    if clear_last_outcome:
        task.runtime.last_outcome = TaskOutcomeState()
    elif task.runtime.last_outcome.kind == "interrupted":
        task.runtime.last_outcome.stage = pipeline_status


def _enqueue_recovered_task(state: WorkspaceState, task_id: str) -> None:
    state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
    state.queue.append(task_id)


def prepare_completed_task_for_recovery(task: TaskRecord, *, recovery_stage: str) -> None:
    _reset_task_for_recovery(
        task,
        status="queued",
        pipeline_status=recovery_stage,
    )
    set_task_commit_sha(task, None)
    task.git.rolled_back_checkpoint_attempt = None


def requeue_task(root: Path, task_id: str, *, front: bool = False) -> TaskRecord:
    with _workspace_lock(root):
        task = require_task(root, task_id)
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status not in {"flagged", "parked", *CLOSED_TASK_STATUSES}:
            raise ValueError(f"Task {task.id} is not flagged, parked, or closed")
        _reset_task_for_recovery(
            task,
            status="queued",
            pipeline_status=implementation_entry_stage(task),
            clear_last_outcome=task.status not in {"flagged", "parked"},
        )
        state.queue = [item for item in state.queue if item != task.id]
        if front:
            state.queue.insert(0, task.id)
        else:
            state.queue.append(task.id)
        _persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message="Task requeued for another implementation pass.",
        )
        return task


def resume_task(root: Path, task_id: str, *, front: bool = False) -> TaskRecord:
    with _workspace_lock(root):
        task = require_task(root, task_id)
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status not in {"flagged", *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}:
            raise ValueError(f"Task {task.id} is not interrupted, parked, flagged, or closed")
        if task.pipeline_status in {"backlog", "done"}:
            raise ValueError(f"Task {task.id} has no resumable stage")
        resumed_stage = task.pipeline_status
        if resumed_stage in {"implementing", "testing", "accepting"}:
            resumed_stage = reroute_stage_for_acceptance_criteria(task)
        _reset_task_for_recovery(
            task,
            status="queued",
            pipeline_status=resumed_stage,
            clear_last_outcome=task.status not in {"interrupted", "parked", "flagged"},
            preserve_continuation_handoff=task.status in {"interrupted", "parked"},
        )
        state.queue = [item for item in state.queue if item != task.id]
        if front:
            state.queue.insert(0, task.id)
        else:
            state.queue.append(task.id)
        _persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=f"Task resumed from `{resumed_stage}`.",
        )
        return task


def abandon_task(root: Path, task_id: str) -> TaskRecord:
    with _workspace_lock(root):
        task = require_task(root, task_id)
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status not in {"flagged", *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}:
            raise ValueError(f"Task {task.id} is not interrupted, parked, flagged, or closed")
        task.status = "cancelled"
        task.runtime.execution_status = "cancelled"
        task.runtime.run_started_at = None
        task.runtime.updated_at = utcnow()
        task.runtime.active_subagent = None
        task.runtime.last_outcome.kind = "cancelled"
        task.runtime.last_outcome.stage = task.pipeline_status
        task.runtime.last_outcome.reason_code = "execution_cancelled"
        task.runtime.last_outcome.reason = "Task abandoned via CLI."
        task.runtime.last_outcome.retry_count = 0
        task.runtime.last_outcome.retry_limit = 0
        task.runtime.last_outcome.retry_source = "global"
        task.runtime.last_outcome.recorded_at = task.runtime.updated_at
        if state.active_task_id == task.id:
            state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        _persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=f"Task abandoned via CLI at stage `{task.pipeline_status}`.",
        )
        return task


_CLOSE_OUTCOME_REASON_CODES = {"wont_do", "deferred", "duplicate", "execution_cancelled"}

_CLOSE_REASON_CODE_LABELS: dict[str, str] = {
    "wont_do": "Task closed as won't do.",
    "deferred": "Task deferred.",
    "duplicate": "Task closed as duplicate.",
    "execution_cancelled": "Task abandoned via CLI.",
}


def close_task(
    root: Path,
    task_id: str,
    *,
    outcome: str,
    reason: str | None = None,
    follow_up_task_id: str | None = None,
) -> TaskRecord:
    """Mark a task as explicitly closed with a non-implementation outcome.

    Valid outcomes: ``wont_do``, ``deferred``, ``duplicate``, ``execution_cancelled``.
    The task is removed from the queue.
    """
    if outcome not in _CLOSE_OUTCOME_REASON_CODES:
        allowed = ", ".join(sorted(_CLOSE_OUTCOME_REASON_CODES))
        raise ValueError(f"Unsupported close outcome '{outcome}'. Expected one of: {allowed}")
    with _workspace_lock(root):
        task = require_task(root, task_id)
        if follow_up_task_id is not None:
            follow_up_task_id = follow_up_task_id.strip()
            if not follow_up_task_id:
                raise ValueError("Follow-up task id must not be empty")
            if follow_up_task_id == task.id:
                raise ValueError(f"Task {task.id} cannot reference itself as a follow-up task")
            require_task(root, follow_up_task_id)
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status == "done":
            raise ValueError(f"Task {task.id} is already done and cannot be closed")
        now = utcnow()
        task.status = outcome  # type: ignore[assignment]
        task.runtime.execution_status = "cancelled"
        task.runtime.run_started_at = None
        task.runtime.updated_at = now
        task.runtime.active_subagent = None
        task.runtime.last_outcome.kind = outcome  # type: ignore[assignment]
        task.runtime.last_outcome.stage = task.pipeline_status
        task.runtime.last_outcome.reason_code = outcome
        task.runtime.last_outcome.reason = reason or _CLOSE_REASON_CODE_LABELS[outcome]
        task.runtime.last_outcome.follow_up_task_id = follow_up_task_id
        task.runtime.last_outcome.retry_count = 0
        task.runtime.last_outcome.retry_limit = 0
        task.runtime.last_outcome.retry_source = "global"
        task.runtime.last_outcome.recorded_at = now
        if state.active_task_id == task.id:
            state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        journal_message = f"Task closed: {outcome}."
        if reason:
            journal_message += f" {reason}"
        if follow_up_task_id is not None:
            journal_message += f" Follow-up task: {follow_up_task_id}."
        _persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=journal_message,
        )
        return task


def park_task(root: Path, task_id: str) -> TaskRecord:
    """Mark a task as parked.

    The task is removed from the queue and set to status 'parked'.
    """
    with _workspace_lock(root):
        task = require_task(root, task_id)
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status == "done":
            raise ValueError(f"Task {task.id} is already done and cannot be parked")
        now = utcnow()
        task.status = "parked"
        task.runtime.execution_status = "paused"
        task.runtime.run_started_at = None
        task.runtime.updated_at = now
        task.runtime.active_subagent = None
        if state.active_task_id == task.id:
            state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        _persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=f"Task parked via CLI at stage `{task.pipeline_status}`.",
        )
        return task


def update_task(
    root: Path,
    task_id: str,
    *,
    depends_on: list[str] | object = ...,
    task_type: str | None | object = ...,
    engine: str | None | object = ...,
    model: str | None | object = ...,
    retry_limit: int | None | object = ...,
    priority: str | object = ...,
    goal: str | object = ...,
    acceptance_criteria: list[str] | object = ...,
    constraints: list[str] | object = ...,
    plan: list[str] | object = ...,
    pm_complexity: str | None | object = ...,
    planned_effort: str | None | object = ...,
    human_checkpoints: list[str] | object = ...,
    mode: str | object = ...,
    auto_commit: bool | object = ...,
    outcome: str | None | object = ...,
    outcome_reason: str | None | object = ...,
    action: str | None | object = ...,
    journal_message: str | None = None,
) -> TaskRecord:
    with _workspace_lock(root):
        state = load_state(root)
        task = require_task(root, task_id)
        # Skip the conflict guard when the current thread is the runner
        # (e.g., apply_task_updates_from_report during grooming).
        owner_thread_id = threading.get_ident()
        with _RUNNER_LOCKS_MUTEX:
            runner_state = _RUNNER_LOCKS.get(root.resolve())
        is_runner_thread = runner_state is not None and runner_state.owner_thread_id == owner_thread_id
        if not is_runner_thread:
            _ensure_future_task_mutation_allowed(root, [task.id], state=state)

        if outcome is not ... and outcome is not None:
            outcome_str = str(outcome)
            reason_str = (
                str(outcome_reason)
                if outcome_reason is not ... and outcome_reason is not None
                else None
            )
            if outcome_str not in _CLOSE_OUTCOME_REASON_CODES:
                allowed = ", ".join(sorted(_CLOSE_OUTCOME_REASON_CODES))
                raise ValueError(
                    f"Unsupported close outcome '{outcome_str}'. Expected one of: {allowed}"
                )
            if task.status == "done":
                raise ValueError(f"Task {task.id} is already done and cannot be closed")
            now = utcnow()
            task.status = outcome_str
            task.pipeline_status = "done"
            task.runtime.execution_status = "cancelled"
            task.runtime.run_started_at = None
            task.runtime.updated_at = now
            task.runtime.active_subagent = None
            task.runtime.last_outcome.kind = outcome_str
            task.runtime.last_outcome.stage = task.pipeline_status
            task.runtime.last_outcome.reason_code = outcome_str
            task.runtime.last_outcome.reason = reason_str or _CLOSE_REASON_CODE_LABELS.get(
                outcome_str, f"Task closed: {outcome_str}."
            )
            task.runtime.last_outcome.retry_count = 0
            task.runtime.last_outcome.retry_limit = 0
            task.runtime.last_outcome.retry_source = "global"
            task.runtime.last_outcome.recorded_at = now
            if state.active_task_id == task.id:
                state.active_task_id = None
            state.queue = [item for item in state.queue if item != task.id]
            close_msg = f"Task closed: {outcome_str}."
            if reason_str:
                close_msg += f" {reason_str}"
            _persist_task_and_state_without_runner_guard(
                root,
                task=task,
                state=state,
                journal_message=close_msg,
            )
            return task

        if action is not ... and action is not None:
            if action == "park":
                if task.status == "done":
                    raise ValueError(f"Task {task.id} is already done and cannot be parked")
                now = utcnow()
                task.status = "parked"
                task.runtime.execution_status = "paused"
                task.runtime.run_started_at = None
                task.runtime.updated_at = now
                task.runtime.active_subagent = None
                if state.active_task_id == task.id:
                    state.active_task_id = None
                state.queue = [item for item in state.queue if item != task.id]
                _persist_task_and_state_without_runner_guard(
                    root,
                    task=task,
                    state=state,
                    journal_message=f"Task parked via structured report at stage `{task.pipeline_status}`.",
                )
                return task
            if action == "requeue":
                if task.status not in {"flagged", "parked", *CLOSED_TASK_STATUSES}:
                    raise ValueError(f"Task {task.id} is not flagged, parked, or closed")
                _reset_task_for_recovery(
                    task,
                    status="queued",
                    pipeline_status=implementation_entry_stage(task),
                    clear_last_outcome=task.status not in {"flagged", "parked"},
                )
                state.queue = [item for item in state.queue if item != task.id]
                state.queue.append(task.id)
                _persist_task_and_state_without_runner_guard(
                    root,
                    task=task,
                    state=state,
                    journal_message="Task requeued for another implementation pass.",
                )
                return task
            if action == "abandon":
                if task.status not in {"flagged", *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}:
                    raise ValueError(f"Task {task.id} is not interruptible or closed")
                now = utcnow()
                task.status = "cancelled"
                task.runtime.execution_status = "cancelled"
                task.runtime.run_started_at = None
                task.runtime.updated_at = now
                task.runtime.active_subagent = None
                task.runtime.last_outcome.kind = "cancelled"
                task.runtime.last_outcome.stage = task.pipeline_status
                task.runtime.last_outcome.reason_code = "execution_cancelled"
                task.runtime.last_outcome.reason = "Task abandoned via structured report."
                task.runtime.last_outcome.retry_count = 0
                task.runtime.last_outcome.retry_limit = 0
                task.runtime.last_outcome.retry_source = "global"
                task.runtime.last_outcome.recorded_at = now
                if state.active_task_id == task.id:
                    state.active_task_id = None
                state.queue = [item for item in state.queue if item != task.id]
                _persist_task_and_state_without_runner_guard(
                    root,
                    task=task,
                    state=state,
                    journal_message=f"Task abandoned via structured report at stage `{task.pipeline_status}`.",
                )
                return task
            raise ValueError(f"Unsupported action '{action}'")

        if depends_on is not ...:
            _validate_task_dependencies(root, task_id=task.id, depends_on=list(depends_on))
            task.depends_on = list(depends_on)

        if task_type is not ...:
            if task_type is not None and task_type not in VALID_TASK_TYPES:
                raise ValueError(f"Unsupported task type '{task_type}'")
            task.task_type = task_type

        if engine is not ...:
            if engine is not None and engine not in VALID_TASK_ENGINES:
                raise ValueError(f"Unsupported engine '{engine}'")
            task.engine = engine

        if model is not ...:
            task.model = model

        if retry_limit is not ...:
            if retry_limit is not None and retry_limit < 0:
                raise ValueError("Retry limit must be 0 or greater")
            task.retry_policy.max_retries = retry_limit

        if priority is not ...:
            if priority not in VALID_TASK_PRIORITIES:
                raise ValueError(f"Unsupported priority '{priority}'")
            task.priority = priority

        if pm_complexity is not ...:
            if pm_complexity is not None and pm_complexity not in VALID_PM_COMPLEXITIES:
                raise ValueError(f"Unsupported PM complexity '{pm_complexity}'")
            task.pm_complexity = pm_complexity  # type: ignore[assignment]

        if planned_effort is not ...:
            if planned_effort is not None and planned_effort not in VALID_PLANNED_EFFORTS:
                raise ValueError(f"Unsupported planned effort '{planned_effort}'")
            task.planned_effort = planned_effort  # type: ignore[assignment]

        if goal is not ...:
            task.goal = goal

        if acceptance_criteria is not ...:
            task.acceptance_criteria = normalize_acceptance_criteria(list(acceptance_criteria))

        if constraints is not ...:
            task.constraints = normalize_task_text_list(list(constraints))

        if plan is not ...:
            task.plan = normalize_task_text_list(list(plan))

        if human_checkpoints is not ...:
            task.human_checkpoints = normalize_human_checkpoints(list(human_checkpoints))

        if mode is not ...:
            if mode not in {"tasks", "implementation"}:
                raise ValueError(f"Unsupported mode '{mode}'")
            task.mode = mode  # type: ignore[assignment]

        if auto_commit is not ...:
            task.git.auto_commit = auto_commit

        apply_task_template_defaults(task)
        task.pipeline_status = reroute_stage_for_acceptance_criteria(task)

        if journal_message is None:
            journal_message = "Task metadata updated via CLI."
        if (
            task.pipeline_status == "grooming"
            and missing_acceptance_criteria_reason(task) is not None
        ):
            journal_message += (
                " Rerouted to `grooming` until structured acceptance criteria are added."
            )
        _persist_future_task_update(root, task, journal_message=journal_message)
        return task


update_task_metadata = update_task


def active_task_markers(root: Path, state: WorkspaceState | None = None) -> dict[str, list[str]]:
    markers: dict[str, list[str]] = {}
    current_state = state or load_state(root)
    if current_state.active_task_id is not None:
        markers.setdefault(current_state.active_task_id, []).append("workspace.active_task_id")
    for task in list_tasks(root):
        if task.runtime.execution_status == "running":
            markers.setdefault(task.id, []).append("runtime.execution_status=running")
    return markers


def _validate_single_active_task(root: Path, state: WorkspaceState | None = None) -> None:
    markers = active_task_markers(root, state)
    if len(markers) <= 1:
        return
    details = "; ".join(
        f"{task_id} ({', '.join(task_markers)})"
        for task_id, task_markers in sorted(markers.items())
    )
    raise WorkspaceConflictError(
        f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
    )
