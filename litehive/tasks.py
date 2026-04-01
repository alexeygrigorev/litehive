"""Task storage helpers for the local YAML workspace."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import re
from contextlib import contextmanager
import os
import shutil
from pathlib import Path
import sys

import yaml

from litehive.config import (
    VALID_TASK_ROUTING_KEYS,
    VALID_POOL_SELECTION_POLICIES,
    ensure_workspace,
    load_config,
    render_workspace_gitignore,
    state_path,
    workspace_dir,
    workspace_gitignore_path,
)
from litehive.git_ops import GitError, checkpoint_message, default_commit_message, find_commit_by_subject, is_git_repo
from litehive.models import (
    FollowUpTaskSpec,
    RuntimeEngineSwitch,
    RuntimeSubagentState,
    StageReport,
    TaskCreationSource,
    SubagentRef,
    TaskOutcomeState,
    TaskRecord,
    TaskRuntime,
    WorkspaceState,
    utcnow,
)

VALID_TASK_PRIORITIES = {"low", "medium", "high"}
VALID_TASK_ENGINES = {"codex", "opencode", "gemini", "copilot", "claude"}
VALID_HUMAN_CHECKPOINTS = {"before_acceptance", "before_commit"}
VALID_TASK_TYPES = set(VALID_TASK_ROUTING_KEYS)
TASK_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_RUNNER_LOCKS: dict[Path, tuple[object, int]] = {}
_MISSING = object()
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
            "Prepare the task for PM grooming.",
        ],
        "prompt_guidance": [
            "Keep the scope high-level; the PM will handle decomposition later.",
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


class WorkspaceConflictError(ValueError):
    """Raised when workspace mutations would conflict with an active runner."""


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
            raise ValueError(f"Unsupported human checkpoint '{checkpoint}'. Expected one of: {allowed}")
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
    if missing_acceptance_criteria_reason(task) is not None:
        return "grooming"
    return "implementing"


def reroute_stage_for_acceptance_criteria(task: TaskRecord) -> str:
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
            inferred.append("The implementation covers the defined plan end-to-end without broad unrelated changes.")
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


def _write_runner_lock_metadata(handle: object, root: Path) -> None:
    metadata = {
        "pid": os.getpid(),
        "workspace": str(root.resolve()),
        "started_at": utcnow(),
        "command": " ".join(sys.argv),
    }
    handle.seek(0)
    handle.truncate()
    handle.write(yaml.safe_dump(metadata, sort_keys=False))
    handle.flush()


def _read_runner_lock_metadata(root: Path) -> dict[str, object]:
    lock_path = runner_lock_path(root)
    if not lock_path.exists():
        return {}
    data = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _runner_conflict_message(root: Path) -> str:
    metadata = _read_runner_lock_metadata(root)
    pid = metadata.get("pid")
    started_at = metadata.get("started_at")
    command = metadata.get("command")
    details = []
    if pid is not None:
        details.append(f"pid={pid}")
    if started_at:
        details.append(f"started_at={started_at}")
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
    existing = _RUNNER_LOCKS.get(root)
    if existing is not None:
        handle, depth = existing
        _RUNNER_LOCKS[root] = (handle, depth + 1)
        try:
            yield
        finally:
            handle, depth = _RUNNER_LOCKS[root]
            if depth <= 1:
                _RUNNER_LOCKS.pop(root, None)
            else:
                _RUNNER_LOCKS[root] = (handle, depth - 1)
        return

    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceConflictError(_runner_conflict_message(root)) from exc
        _write_runner_lock_metadata(handle, root)
        _RUNNER_LOCKS[root] = (handle, 1)
        try:
            yield
        finally:
            _RUNNER_LOCKS.pop(root, None)
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def workspace_mutation_guard(root: Path):
    root = root.resolve()
    if root in _RUNNER_LOCKS:
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


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "task"


def _next_task_id(root: Path) -> str:
    existing = []
    for child in tasks_root(root).iterdir():
        if not child.is_dir():
            continue
        match = re.match(r"^T-(\d{4})-", child.name)
        if match:
            existing.append(int(match.group(1)))
    next_number = max(existing, default=0) + 1
    return f"T-{next_number:04d}"


def task_dir(root: Path, task: TaskRecord) -> Path:
    return tasks_root(root) / f"{task.id}-{task.slug}"


def task_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "task.yaml"


def task_runtime_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "runtime.yaml"


def _ensure_runtime_ignored(root: Path) -> None:
    ignore_path = workspace_gitignore_path(root)
    expected = render_workspace_gitignore()
    if not ignore_path.exists() or ignore_path.read_text(encoding="utf-8") != expected:
        ignore_path.write_text(expected, encoding="utf-8")


def _serialize_task_record(task: TaskRecord) -> str:
    task_payload = task.model_dump(mode="python")
    task_payload["git"]["commit_sha"] = None
    return yaml.safe_dump(task_payload, sort_keys=False)


def _serialize_task_runtime(task: TaskRecord) -> str:
    return yaml.safe_dump(
        {
            **task.runtime.model_dump(mode="python"),
            "git": {"commit_sha": task.git.commit_sha},
        },
        sort_keys=False,
    )


def _write_task_runtime(root: Path, task: TaskRecord) -> None:
    _atomic_write_text(task_runtime_file(root, task), _serialize_task_runtime(task))
    _ensure_runtime_ignored(root)


def set_task_commit_sha(task: TaskRecord, commit_sha: str | None) -> None:
    task.git.commit_sha = commit_sha
    task.runtime.git.commit_sha = commit_sha


def save_task_runtime(root: Path, task: TaskRecord) -> None:
    with workspace_mutation_guard(root):
        _write_task_runtime(root, task)


def _load_task_runtime(root: Path, task: TaskRecord) -> TaskRecord:
    runtime_file = task_runtime_file(root, task)
    if not runtime_file.exists():
        return task
    data = yaml.safe_load(runtime_file.read_text(encoding="utf-8")) or {}
    task.runtime = TaskRuntime(**data)
    set_task_commit_sha(task, task.runtime.git.commit_sha)
    return task


def create_task(
    root: Path,
    *,
    title: str,
    depends_on: list[str] | None = None,
    mode: str = "implementation",
    task_type: str | None = None,
    engine: str | None = None,
    model: str | None = None,
    retry_limit: int | None = None,
    goal: str = "",
    acceptance_criteria: list[str] | None = None,
    human_checkpoints: list[str] | None = None,
    auto_commit: bool = True,
) -> TaskRecord:
    ensure_workspace(root)
    if retry_limit is not None and retry_limit < 0:
        raise ValueError("Retry limit must be 0 or greater")
    if task_type is not None and task_type not in VALID_TASK_TYPES:
        raise ValueError(f"Unsupported task type '{task_type}'")
    with _workspace_lock(root):
        task_id = _next_task_id(root)
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
            goal=goal,
            acceptance_criteria=normalize_acceptance_criteria(acceptance_criteria),
            human_checkpoints=normalize_human_checkpoints(human_checkpoints),
            retry_policy={"max_retries": retry_limit},
            git={
                "auto_commit": auto_commit,
                "commit_message": default_commit_message(task_id, slug),
            },
        )
        task = apply_task_template_defaults(task)

        base = task_dir(root, task)
        (base / "reports").mkdir(parents=True, exist_ok=False)
        (base / "subagents").mkdir(parents=True, exist_ok=False)
        (base / "artifacts").mkdir(parents=True, exist_ok=False)
        state = load_state(root)
        state.queue.append(task.id)
        try:
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
            _write_atomic_files(
                writes
            )
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
        next_number = max(
            (
                int(match.group(1))
                for child in tasks_root(root).iterdir()
                if child.is_dir()
                and (match := re.match(r"^T-(\d{4})-", child.name)) is not None
            ),
            default=0,
        )
        writes: dict[Path, str] = {}

        for follow_up in follow_ups:
            next_number += 1
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
            writes[
                base / "journal.md"
            ] = (
                f"# {task.id} {task.title}\n\n"
                f"## {utcnow()}\n"
                "Task created.\n\n"
                f"Created as a follow-up from `{parent_task.id}` during `{stage}`.\n"
                f"Rationale: {follow_up.rationale}\n"
            )
            if task.mode == "tasks":
                writes[task_brief_file(root, task)] = render_task_brief(task)
            created_tasks.append(task)

        writes[state_path(root)] = _serialize_state(state)
        try:
            _write_atomic_files(writes)
        except Exception:
            for base in reversed(created_dirs):
                shutil.rmtree(base, ignore_errors=True)
            raise
        _ensure_runtime_ignored(root)
    return created_tasks


def list_tasks(root: Path) -> list[TaskRecord]:
    records: list[TaskRecord] = []
    for child in sorted(tasks_root(root).iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        records.append(_load_task_runtime(root, TaskRecord(**data)))
    return records


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
        writes[state_path(root)] = _serialize_state(state)
    return writes


def populate_missing_acceptance_criteria_from_report(
    root: Path, task: TaskRecord, feedback: str
) -> list[str]:
    if task.acceptance_criteria:
        return []
    acceptance_criteria = normalize_acceptance_criteria(
        extract_report_list_section(feedback, "ACCEPTANCE_CRITERIA")
    )
    journal_message = "Acceptance criteria auto-populated from grooming output."
    if not acceptance_criteria:
        acceptance_criteria = infer_acceptance_criteria(task)
        journal_message = "Acceptance criteria inferred from task context after grooming."
    if not acceptance_criteria:
        return []

    task.acceptance_criteria = acceptance_criteria
    save_task(root, task)
    if task.mode == "tasks":
        _atomic_write_text(task_brief_file(root, task), render_task_brief(task))
    append_journal(
        root,
        task,
        journal_message,
    )
    return acceptance_criteria


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
        if state.active_task_id == task.id:
            state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        if final_status in {"paused", "queued"} and task.status == "queued" and task.pipeline_status != "done":
            state.queue.insert(0, task.id)
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
) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.last_outcome = TaskOutcomeState(
        kind=kind,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
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
    )
    save_task_runtime(root, task)


def mark_subagent_pid(root: Path, task: TaskRecord, pid: int | None) -> None:
    if pid is None or task.runtime.active_subagent is None or task.runtime.active_subagent.pid == pid:
        return
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.active_subagent = task.runtime.active_subagent.model_copy(update={"pid": pid, "updated_at": now})
    save_task_runtime(root, task)


def mark_subagent_finished(
    root: Path,
    task: TaskRecord,
    ref: SubagentRef,
    transcript: str,
    exit_code: int,
    pid: int | None = None,
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
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        _validate_single_active_task(root, state)
        next_task, blocked, mutated = _resolve_next_task_from_state(root, state)
        if mutated:
            save_state(root, state)
        return TaskSelection(task=next_task, blocked=blocked)


def plan_task_selections(root: Path) -> TaskPlan:
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        _validate_single_active_task(root, state)
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
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        _validate_single_active_task(root, state)
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
            if next_task.status == "queued":
                next_task.status = "in_progress"
            persist_task_and_state(root, task=next_task, state=state)
        return TaskSelection(task=next_task, blocked=blocked)


def _is_task_eligible_for_execution(task: TaskRecord) -> bool:
    return task.status in {"queued", "in_progress"} and task.pipeline_status != "done"


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
    reverse_dependencies: dict[str, set[str]] = {candidate_id: set() for candidate_id in eligible_task_ids}
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
        return (interrupted_rank, TASK_PRIORITY_ORDER.get(task.priority, 1), queue_index, task.id)
    if policy == "dependency_aware":
        return (
            interrupted_rank,
            -_dependent_task_count(task.id, queue, tasks_by_id),
            queue_index,
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
        and task.status in {"queued", "in_progress"}
        and state.active_task_id != task.id
        and task.id not in state.queue
    )


def _should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    return task.pipeline_status == "commit_to_git" and task.status in {"queued", "in_progress"}


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


def _recover_commit_task(task: TaskRecord) -> str:
    _prepare_recovered_commit_task(task)
    return "Recovered interrupted `commit_to_git` attempt and requeued the task at `commit_to_git`."


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
        (
            "Recovered existing checkpoint commit after interrupted `commit_to_git` "
            f"and finalized the task at `{commit_sha}`."
        )
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
    orphaned = [task for task in tasks if _is_orphaned_commit_stage_task(task, state)]
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
    recovered.extend(orphaned)
    recovered_ids = {task.id for task in recovered}
    resolved_ids = {*recovered_ids, *completed_ids}
    queue = [task_id for task_id in state.queue if task_id not in recovered_ids]
    for task in recovered:
        journal_messages[task.id] = _recover_commit_task(task)
        transitioned.append(task)
        queue.insert(0, task.id)
    queue = [task_id for task_id in queue if task_id not in completed_ids]
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


def _resolve_next_task_from_snapshot(
    state: WorkspaceState,
    tasks_by_id: dict[str, TaskRecord],
    *,
    policy: str,
) -> tuple[TaskRecord | None, list[BlockedTask], bool]:
    mutated = False
    blocked: list[BlockedTask] = []
    blocked_task_ids: set[str] = set()
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
        if state.active_task_id is None:
            return state

        task = get_task(root, state.active_task_id)
        if task is not None and _is_stranded_commit_task(task):
            commit_sha = _find_existing_checkpoint_commit(root, task)
            state.active_task_id = None
            state.queue = [item for item in state.queue if item != task.id]
            if commit_sha is None:
                _prepare_recovered_commit_task(task)
                state.queue.insert(0, task.id)
                persist_task_and_state(
                    root,
                    task=task,
                    state=state,
                    journal_message=(
                        "Recovered interrupted `commit_to_git` attempt and requeued the task "
                        "at `commit_to_git`."
                    ),
                )
                return state

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
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=(
                    "Recovered existing checkpoint commit after interrupted `commit_to_git` "
                    f"and finalized the task at `{commit_sha}`."
                ),
            )
            return state

        if task is not None and _should_requeue_commit_stage_task(task):
            _prepare_recovered_commit_task(task)
            state.queue = [item for item in state.queue if item != task.id]
            state.queue.insert(0, task.id)
            state.active_task_id = None
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=(
                    "Recovered interrupted `commit_to_git` attempt and requeued the task at "
                    "`commit_to_git`."
                ),
            )
            return state

        if task is not None and _is_task_eligible_for_execution(task):
            _prepare_interrupted_task_for_requeue(task)
            state.queue = [item for item in state.queue if item != task.id]
            state.queue.insert(0, task.id)
            state.active_task_id = None
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=(
                    "Recovered interrupted run and requeued the task at "
                    f"`{task.pipeline_status}`."
                ),
            )
            return state

        state.active_task_id = None
        save_state(root, state)
        return state


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
) -> None:
    now = utcnow()
    task.status = status
    task.pipeline_status = pipeline_status
    task.runtime.execution_status = "idle"
    task.runtime.run_started_at = None
    task.runtime.updated_at = now
    task.runtime.active_subagent = None
    task.runtime.retry_count = 0
    task.runtime.retry_limit = 0
    task.runtime.retry_source = "global"
    task.runtime.last_outcome = TaskOutcomeState()


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
        if task.status not in {"flagged", "cancelled", "failed"}:
            raise ValueError(f"Task {task.id} is not flagged, failed, or cancelled")
        _reset_task_for_recovery(
            task,
            status="queued",
            pipeline_status=implementation_entry_stage(task),
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
        if task.status not in {"flagged", "cancelled", "failed"}:
            raise ValueError(f"Task {task.id} is not flagged, failed, or cancelled")
        if task.pipeline_status in {"backlog", "done"}:
            raise ValueError(f"Task {task.id} has no resumable stage")
        resumed_stage = task.pipeline_status
        if resumed_stage in {"implementing", "testing", "accepting"}:
            resumed_stage = reroute_stage_for_acceptance_criteria(task)
        _reset_task_for_recovery(task, status="queued", pipeline_status=resumed_stage)
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
        if task.status not in {"flagged", "failed", "cancelled"}:
            raise ValueError(f"Task {task.id} is not flagged, failed, or cancelled")
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
) -> TaskRecord:
    """Mark a task as cancelled with an explicit non-implementation outcome.

    Valid outcomes: ``wont_do``, ``deferred``, ``duplicate``, ``execution_cancelled``.
    The task is removed from the queue.
    """
    if outcome not in _CLOSE_OUTCOME_REASON_CODES:
        allowed = ", ".join(sorted(_CLOSE_OUTCOME_REASON_CODES))
        raise ValueError(f"Unsupported close outcome '{outcome}'. Expected one of: {allowed}")
    with _workspace_lock(root):
        task = require_task(root, task_id)
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status == "done":
            raise ValueError(f"Task {task.id} is already done and cannot be closed")
        now = utcnow()
        task.status = "cancelled"
        task.runtime.execution_status = "cancelled"
        task.runtime.run_started_at = None
        task.runtime.updated_at = now
        task.runtime.active_subagent = None
        task.runtime.last_outcome.kind = "cancelled"
        task.runtime.last_outcome.stage = task.pipeline_status
        task.runtime.last_outcome.reason_code = outcome
        task.runtime.last_outcome.reason = reason or _CLOSE_REASON_CODE_LABELS[outcome]
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
            journal_message=f"Task closed: {outcome}."
            + (f" {reason}" if reason else ""),
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
    human_checkpoints: list[str] | object = ...,
    mode: str | object = ...,
    auto_commit: bool | object = ...,
) -> TaskRecord:
    with _workspace_lock(root):
        state = load_state(root)
        task = require_task(root, task_id)
        _ensure_future_task_mutation_allowed(root, [task.id], state=state)

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

        if goal is not ...:
            task.goal = goal

        if acceptance_criteria is not ...:
            task.acceptance_criteria = normalize_acceptance_criteria(list(acceptance_criteria))

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

        journal_message = "Task metadata updated via CLI."
        if task.pipeline_status == "grooming" and missing_acceptance_criteria_reason(task) is not None:
            journal_message += " Rerouted to `grooming` until structured acceptance criteria are added."
        _persist_future_task_update(root, task, journal_message=journal_message)
        return task


update_task_metadata = update_task


def active_task_markers(
    root: Path, state: WorkspaceState | None = None
) -> dict[str, list[str]]:
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
        f"{task_id} ({', '.join(task_markers)})" for task_id, task_markers in sorted(markers.items())
    )
    raise WorkspaceConflictError(
        f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
    )
