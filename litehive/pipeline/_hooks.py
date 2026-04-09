"""Runner hook execution for stage boundaries."""

import os
import subprocess
from pathlib import Path

import yaml

from litehive.config import LitehiveConfig
from litehive.models import StageReport, TaskRecord, cap_feedback
from litehive.tasks.journal import append_journal
from litehive.tasks.paths import task_dir
from litehive.tasks.persistence import _atomic_write_gzip_text, _atomic_write_text

_COMPRESS_HOOK_ARTIFACT_MIN_BYTES = 4096

_PRE_STAGE_HOOK_POINTS = {
    "grooming": "before_grooming",
    "implementing": "before_implementing",
    "testing": "before_testing",
    "accepting": "before_accepting",
}
_POST_STAGE_HOOK_POINTS = {
    "grooming": "after_grooming",
    "implementing": "after_implementing",
    "testing": "after_testing",
}
_POST_ACCEPT_VERDICTS = {"pass", "accept"}
_POST_ACCEPT_HOOK_POINT = "after_accepting"
_POST_MERGE_HOOK_POINT = "after_commit"


def _collect_changed_hook_paths(execution_root: Path) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "litehive",
            "tests",
        ],
        cwd=execution_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []

    changed_paths: list[str] = []
    seen: set[str] = set()
    for raw_line in completed.stdout.splitlines():
        if len(raw_line) < 4:
            continue
        candidate = raw_line[3:]
        if " -> " in candidate:
            candidate = candidate.split(" -> ", 1)[1]
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            continue
        resolved = execution_root / candidate
        if not resolved.is_file():
            continue
        seen.add(candidate)
        changed_paths.append(candidate)
    return changed_paths


def _runner_hook_env(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    *,
    step: str,
    hook_point: str,
) -> dict[str, str]:
    changed_paths = _collect_changed_hook_paths(execution_root)
    return {
        **os.environ,
        "LITEHIVE_TASK_ID": task.id,
        "LITEHIVE_TASK_STEP": step,
        "LITEHIVE_HOOK_POINT": hook_point,
        "LITEHIVE_WORKSPACE_ROOT": str(root),
        "LITEHIVE_EXECUTION_ROOT": str(execution_root),
        "LITEHIVE_CHANGED_PATHS": "\n".join(changed_paths),
    }


def _run_runner_hooks_for_stage(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    *,
    step: str,
    config: LitehiveConfig,
    phase: str,
    report: StageReport | None = None,
    collected_results: list[dict[str, str | int | bool | None]] | None = None,
) -> StageReport | None:
    hook_point = _runner_hook_point(step=step, phase=phase, report=report)
    if hook_point is None:
        return None
    configured_hooks = config.runner_hooks.get(hook_point, [])
    if not configured_hooks:
        return None

    for index, hook in enumerate(configured_hooks, start=1):
        hook_result = _execute_runner_hook(
            root,
            execution_root,
            task,
            step=step,
            hook_point=hook_point,
            command=hook.command,
            reject_on_failure=hook.reject_on_failure,
            description=hook.description,
            ordinal=index,
        )
        if report is None:
            if collected_results is not None:
                collected_results.append(hook_result)
            if hook_result["status"] == "failed" and hook.reject_on_failure:
                blocking_results = list(collected_results or [hook_result])
                return _hook_failure_report(
                    task,
                    step=step,
                    hook_result=hook_result,
                    hook_results=blocking_results,
                )
            continue

        report.hook_results.append(hook_result)
        report.warnings = [*report.warnings, *_runner_hook_warnings(hook_result)]
        report.feedback = "\n\n".join(
            part for part in [report.feedback, _runner_hook_feedback(hook_result)] if part
        ).strip()
        if hook_result["status"] == "failed" and hook.reject_on_failure:
            report.verdict = "reject"
            report.source = "hook"
            report.summary = _hook_failure_summary(step=step, hook_result=hook_result)
            report.feedback = _hook_failure_feedback(hook_result)
            report.failure_classification = "runner_hook_failed"
            report.failure_diagnostics = _hook_failure_diagnostics(hook_result)
            return report

    return None


def _attach_runner_hook_results(
    report: StageReport,
    hook_results: list[dict[str, str | int | bool | None]],
) -> None:
    if not hook_results:
        return
    report.hook_results.extend(hook_results)
    report.warnings = [
        *_flatten_runner_hook_warnings(hook_results),
        *report.warnings,
    ]
    report.feedback = cap_feedback(
        "\n\n".join(
            [
                *_flatten_runner_hook_feedback(hook_results),
                report.feedback,
            ]
        ).strip()
    )


def _runner_hook_point(
    *,
    step: str,
    phase: str,
    report: StageReport | None,
) -> str | None:
    if phase == "before":
        return _PRE_STAGE_HOOK_POINTS.get(step)
    if phase == "after":
        return _POST_STAGE_HOOK_POINTS.get(step)
    if phase == "after_accept" and step == "accepting" and report is not None:
        if report.verdict in _POST_ACCEPT_VERDICTS:
            return _POST_ACCEPT_HOOK_POINT
    if phase == "after_merge" and step == "commit_to_git" and report is not None:
        if report.verdict == "pass":
            return _POST_MERGE_HOOK_POINT
    return None


def _execute_runner_hook(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    *,
    step: str,
    hook_point: str,
    command: str,
    reject_on_failure: bool,
    description: str | None,
    ordinal: int,
) -> dict[str, str | int | bool | None]:
    env = _runner_hook_env(
        root,
        execution_root,
        task,
        step=step,
        hook_point=hook_point,
    )
    completed = subprocess.run(
        ["bash", "-lc", command],
        cwd=execution_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    artifact_name = f"{hook_point}-{ordinal:03d}.yaml"
    artifact_path = task_dir(root, task) / "artifacts" / artifact_name
    artifact_payload = {
        "step": step,
        "hook_point": hook_point,
        "command": command,
        "reject_on_failure": reject_on_failure,
        "description": description,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    artifact_content = yaml.safe_dump(artifact_payload, sort_keys=False)
    if len(artifact_content.encode("utf-8")) >= _COMPRESS_HOOK_ARTIFACT_MIN_BYTES:
        artifact_path = artifact_path.with_name(f"{artifact_path.name}.gz")
        _atomic_write_gzip_text(artifact_path, artifact_content)
    else:
        _atomic_write_text(artifact_path, artifact_content)
    artifact_label = artifact_path.relative_to(task_dir(root, task)).as_posix()
    status = "passed" if completed.returncode == 0 else "failed"
    append_journal(
        root,
        task,
        "\n".join(
            [
                f"Runner hook `{hook_point}` {status}: `{command}`.",
                f"- step: `{step}`",
                f"- reject_on_failure: `{reject_on_failure}`",
                f"- description: `{description or '-'}`",
                f"- exit_code: `{completed.returncode}`",
                f"- artifact: `{artifact_label}`",
            ]
        ),
    )
    return {
        "point": hook_point,
        "command": command,
        "reject_on_failure": reject_on_failure,
        "description": description,
        "exit_code": completed.returncode,
        "status": status,
        "artifact": artifact_label,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _runner_hook_warnings(hook_result: dict[str, str | int | bool | None]) -> list[str]:
    qualifier = "passed" if hook_result["status"] == "passed" else "failed"
    return [
        (
            f"runner hook {qualifier}: `{hook_result['point']}` "
            f"`{hook_result['command']}` (artifact: `{hook_result['artifact']}`)"
        )
    ]


def _runner_hook_feedback(hook_result: dict[str, str | int | bool | None]) -> str:
    return (
        f"Runner hook `{hook_result['point']}` `{hook_result['command']}` "
        f"{hook_result['status']} with exit code {hook_result['exit_code']} "
        f"(artifact: `{hook_result['artifact']}`)."
    )


def _hook_failure_summary(*, step: str, hook_result: dict[str, str | int | bool | None]) -> str:
    return (
        f"{step} rejected by runner hook `{hook_result['point']}` "
        f"(exit {hook_result['exit_code']}): {hook_result['command']}"
    )


def _hook_failure_feedback(hook_result: dict[str, str | int | bool | None]) -> str:
    stdout = str(hook_result.get("stdout") or "").rstrip()
    stderr = str(hook_result.get("stderr") or "").rstrip()
    lines = [
        f"Runner hook failed: `{hook_result['point']}`",
        f"Command: {hook_result['command']}",
        f"Exit code: {hook_result['exit_code']}",
    ]
    description = hook_result.get("description")
    if description:
        lines.append(f"Check: {description}")
    lines.extend(
        [
            "",
            "stdout:",
            stdout or "(empty)",
            "",
            "stderr:",
            stderr or "(empty)",
        ]
    )
    return cap_feedback("\n".join(lines).strip())


def _hook_failure_diagnostics(
    hook_result: dict[str, str | int | bool | None],
) -> dict[str, str | int | bool | None | list[str]]:
    return {
        "hook_point": hook_result["point"],
        "command": hook_result["command"],
        "description": hook_result.get("description"),
        "exit_code": hook_result["exit_code"],
        "artifact": hook_result["artifact"],
        "stdout": hook_result.get("stdout"),
        "stderr": hook_result.get("stderr"),
    }


def _hook_failure_report(
    task: TaskRecord,
    *,
    step: str,
    hook_result: dict[str, str | int | bool | None],
    hook_results: list[dict[str, str | int | bool | None]],
) -> StageReport:
    return StageReport(
        task_id=task.id,
        step=step,  # type: ignore[arg-type]
        verdict="reject",
        source="hook",
        summary=_hook_failure_summary(step=step, hook_result=hook_result),
        feedback=_hook_failure_feedback(hook_result),
        warnings=_flatten_runner_hook_warnings(hook_results),
        hook_results=hook_results,
        failure_classification="runner_hook_failed",
        failure_diagnostics=_hook_failure_diagnostics(hook_result),
    )


def _flatten_runner_hook_warnings(
    hook_results: list[dict[str, str | int | bool | None]],
) -> list[str]:
    warnings: list[str] = []
    for hook_result in hook_results:
        warnings.extend(_runner_hook_warnings(hook_result))
    return warnings


def _flatten_runner_hook_feedback(
    hook_results: list[dict[str, str | int | bool | None]],
) -> list[str]:
    return [_runner_hook_feedback(hook_result) for hook_result in hook_results]
