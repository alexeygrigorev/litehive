"""Model and engine resolution, retry policy, and continuation handoff."""

import re
from pathlib import Path

from litehive.config import ExecutionRetryPolicy, LitehiveConfig
from litehive.engines import extract_engine_continuation, get_engine
from litehive.models import RuntimeContinuationHandoff, StageReport, TaskRecord
from litehive.tasks import mark_engine_switch, set_task_continuation_handoff

from ._budget import _engine_attempt_order
from ._types import ResolvedExecutionRetryPolicy


def workspace_model_for_engine(config: LitehiveConfig, engine_name: str) -> str | None:
    if engine_name == "codex":
        return config.codex_model
    if engine_name == "opencode":
        return config.opencode_model
    if engine_name == "goz":
        return config.goz_model
    if engine_name == "gemini":
        return config.gemini_model
    if engine_name == "copilot":
        return config.copilot_model
    if engine_name == "claude":
        return config.claude_model
    return None


def resolve_model(
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_name: str,
    model_override: str | None = None,
) -> str | None:
    if not get_engine(engine_name).capabilities.supports_model_override:
        return None
    if model_override is not None:
        return model_override
    if task.model is not None:
        return task.model
    return workspace_model_for_engine(config, engine_name)


def resolve_engine_name(
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_override: str | None = None,
) -> str:
    return resolve_engine_plan(task, config, engine_override=engine_override)[0]


def resolve_engine_attempt_order(
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_override: str | None = None,
) -> list[str]:
    return _engine_attempt_order(
        resolve_engine_plan(task, config, engine_override=engine_override),
        config.engine_fallbacks,
    )


def resolve_engine_plan(
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_override: str | None = None,
) -> list[str]:
    if engine_override is not None:
        return [engine_override]
    if task.engine is not None:
        return [task.engine]
    routed_engines = _route_engines_for_task(task, config)
    if routed_engines:
        return routed_engines
    return [config.default_engine]


def _route_engines_for_task(task: TaskRecord, config: LitehiveConfig) -> list[str]:
    route_key = _task_routing_key(task)
    if route_key is None:
        return []

    engines: list[str] = []
    seen: set[str] = set()
    for engine_name in config.task_engine_routing.get(route_key, []):
        if engine_name in seen:
            continue
        seen.add(engine_name)
        engines.append(engine_name)
    return engines


def _task_routing_key(task: TaskRecord) -> str | None:
    if task.task_type is not None:
        return task.task_type

    text = " ".join(
        [
            task.title,
            task.goal,
            *task.acceptance_criteria,
            *task.constraints,
            *task.plan,
        ]
    ).lower()
    if not text.strip():
        return None

    routing_patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("adapter", ("adapter", "integration", "provider", "cli adapter", "engine adapter")),
        ("bugfix", ("bugfix", "bug fix", "fix ", "fixes ", "fixing ", "regression", "hotfix", "flaky")),
        ("review", ("review", "review feedback", "requested changes", "triage", "comments")),
        ("research", ("research", "investigate", "investigation", "spike", "analyze", "analysis", "explore", "evaluate")),
        ("refactor", ("refactor", "cleanup", "clean up", "rename", "reorganize", "extract", "simplify")),
        ("docs", (" docs", "doc ", "documentation", "document ", "readme", "guide")),
    )
    padded = f" {text} "
    for route_key, patterns in routing_patterns:
        if any(pattern in padded for pattern in patterns):
            return route_key
    if re.search(r"\bfix\b", text):
        return "bugfix"
    if re.search(r"\bdoc(s)?\b", text):
        return "docs"
    return None


def resolve_task_retry_policy(task: TaskRecord, config: LitehiveConfig) -> tuple[int, str]:
    if task.retry_policy.max_retries is not None:
        return task.retry_policy.max_retries, "task"
    return config.default_retry_limit, "global"


def _resolve_stage_retry_limit(task: TaskRecord, config: LitehiveConfig) -> int:
    if task.retry_policy.stage_retry_limit is not None:
        return task.retry_policy.stage_retry_limit
    return config.default_stage_retry_limit


def _execution_retry_model_family(*, engine_name: str, model_name: str | None) -> str:
    if model_name:
        model_tail = model_name.rsplit("/", 1)[-1].strip().lower()
        match = re.match(r"[a-z0-9]+", model_tail)
        if match is not None:
            return match.group(0)
    return engine_name


def resolve_execution_retry_policy(
    config: LitehiveConfig, *, engine_name: str, model_name: str | None = None
) -> ResolvedExecutionRetryPolicy:
    model_family = _execution_retry_model_family(engine_name=engine_name, model_name=model_name)
    selector_order = [engine_name, f"model_family:{model_family}", "external_cli"]
    for selector in selector_order:
        if selector in config.execution_retry_policies:
            return ResolvedExecutionRetryPolicy(
                selector=selector,
                policy=config.execution_retry_policies[selector],
            )
    return ResolvedExecutionRetryPolicy(selector="none", policy=ExecutionRetryPolicy())


def _retry_backoff_seconds(policy: ExecutionRetryPolicy, retry_number: int) -> float:
    if retry_number <= 0:
        return 0.0
    return policy.backoff_seconds * (policy.backoff_multiplier ** (retry_number - 1))


def _set_continuation_handoff(
    root: Path,
    task: TaskRecord,
    *,
    step: str,
    kind: str,
    reason: str,
    result,
    from_engine: str,
    to_engine: str | None,
    from_model: str | None,
    to_model: str | None,
    attempt: int,
) -> RuntimeContinuationHandoff:
    transcript_snippet = ""
    summary = ""
    warnings: list[str] = []
    if result.transcript:
        transcript_snippet = result.transcript.splitlines()[0].strip()
    if result.execution is not None:
        rendered = get_engine(from_engine).render_transcript(result.execution)
        transcript_snippet = transcript_snippet or rendered.splitlines()[0].strip()
        if rendered.strip():
            report = get_engine(from_engine).parse_stage_report(
                task_id=task.id,
                step=step,  # type: ignore[arg-type]
                execution=result.execution,
                subagent_status=result.ref.status,
            )
            summary = report.summary
            warnings = list(report.warnings)

    handoff = RuntimeContinuationHandoff(
        step=step,
        kind=kind,  # type: ignore[arg-type]
        reason=reason,
        from_engine=from_engine,
        to_engine=to_engine,
        from_model=from_model,
        to_model=to_model,
        subagent_id=result.ref.id,
        subagent_path=result.ref.path,
        status=result.ref.status,
        attempt=attempt,
        summary=summary,
        transcript_snippet=transcript_snippet,
        warnings=warnings,
        session_path=f"{result.ref.path}/session.yaml",
        report_path=f"{result.ref.path}/report.yaml",
        transcript_path=f"{result.ref.path}/transcript.md",
        continuation=extract_engine_continuation(from_engine, result.execution),
    )
    set_task_continuation_handoff(root, task, handoff)
    return handoff


def _is_recovery_run(task: TaskRecord) -> bool:
    if task.runtime.continuation_handoff is not None:
        return True
    return task.runtime.last_outcome.kind in {"flagged", "interrupted"}


def _role_for_step(step: str, task: TaskRecord | None = None) -> str:
    if task is not None and step in {"implementing", "testing", "accepting"} and _is_recovery_run(task):
        return "recovery"
    return {
        "grooming": "planner",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "reviewer",
    }.get(step, "swe")
