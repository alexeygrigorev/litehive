"""Runner hook execution for stage boundaries."""

import subprocess
from pathlib import Path

import yaml

from litehive.config import LitehiveConfig
from litehive.models import StageReport, TaskRecord, cap_feedback
from litehive.tasks import (
    _atomic_write_gzip_text,
    _atomic_write_text,
    append_journal,
    task_dir,
)

_COMPRESS_HOOK_ARTIFACT_MIN_BYTES = 4096

_PRE_STAGE_HOOK_POINTS = {
    "implementing": "before_swe_implementation",
    "accepting": "before_pm_acceptance",
}
_POST_STAGE_HOOK_POINTS = {
    "implementing": "after_swe_implementation",
}
_POST_ACCEPT_VERDICTS = {"pass", "accept"}


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
            blocking=hook.blocking,
            ordinal=index,
        )
        if report is None:
            if collected_results is not None:
                collected_results.append(hook_result)
            if hook_result["status"] == "failed" and hook.blocking:
                blocking_results = list(collected_results or [hook_result])
                return StageReport(
                    task_id=task.id,
                    step=step,  # type: ignore[arg-type]
                    verdict="blocked",
                    summary=(
                        f"{step} blocked by runner hook `{hook_point}` "
                        f"(exit {hook_result['exit_code']}): {hook.command}"
                    ),
                    feedback="\n\n".join(_flatten_runner_hook_feedback(blocking_results)),
                    warnings=_flatten_runner_hook_warnings(blocking_results),
                    hook_results=blocking_results,
                )
            continue

        report.hook_results.append(hook_result)
        report.warnings = [*report.warnings, *_runner_hook_warnings(hook_result)]
        report.feedback = "\n\n".join(
            part for part in [report.feedback, _runner_hook_feedback(hook_result)] if part
        ).strip()
        if hook_result["status"] == "failed" and hook.blocking:
            report.verdict = "blocked"
            report.summary = (
                f"{step} blocked by runner hook `{hook_point}` "
                f"(exit {hook_result['exit_code']}): {hook.command}"
            )
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
            return "after_pm_acceptance"
    return None


def _execute_runner_hook(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    *,
    step: str,
    hook_point: str,
    command: str,
    blocking: bool,
    ordinal: int,
) -> dict[str, str | int | bool | None]:
    completed = subprocess.run(
        ["bash", "-lc", command],
        cwd=execution_root,
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
        "blocking": blocking,
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
                f"- blocking: `{blocking}`",
                f"- exit_code: `{completed.returncode}`",
                f"- artifact: `{artifact_label}`",
            ]
        ),
    )
    return {
        "point": hook_point,
        "command": command,
        "blocking": blocking,
        "exit_code": completed.returncode,
        "status": status,
        "artifact": artifact_label,
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
