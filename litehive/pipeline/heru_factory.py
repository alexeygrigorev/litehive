"""HeruEngineFactory — produces ``Engine`` instances backed by heru.

The factory takes a workspace root and returns a callable
``Callable[[str], Engine]`` suitable for the ``ConfigBackedEngineSelector``.
Each call to the inner callable produces a fresh ``HeruEngineAdapter`` for
the requested engine name.

The adapter delegates the actual subagent invocation to ``SubagentManager``
(``litehive.agents.SubagentManager``) so we don't reimplement heru's CLI
shelling, transcript handling, or session management. We sit on top of it,
translating to/from the v2 contract:

  - prompt dict → serialized string via ``serialize_prompt``
  - SubagentResult → ``AgentVerdict`` via the verdict reader, which checks
    whether a fresh ``litehive report`` submission landed in the workspace
    journal during this turn
  - heru exceptions → error taxonomy
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from litehive.agents import SubagentManager
from litehive.tasks.crud import get_task

from .nodes.agent import (
    AgentVerdict,
    Engine,
    EngineOverloaded,
    NudgeRequired,
    QuotaExceeded,
    TransientError,
    UnrecoverableError,
)
from .persistence import TaskState
from .prompt_serializer import serialize_prompt
from .sessions import Session


class _MissingThreadComment(Exception):
    """Internal: agent finished without producing a fresh thread comment."""


def _latest_verdict_after(
    workspace_root: Path,
    task_id: str,
    step: str,
    after_ts: datetime,
) -> AgentVerdict | None:
    """Return the most recent thread comment for ``(task_id, step)`` whose
    ``created_at`` is newer than ``after_ts``, mapped to an ``AgentVerdict``.

    Returns ``None`` when nothing newer landed — caller raises ``NudgeRequired``.
    """
    from litehive.tasks.reports import load_task_thread

    task = get_task(workspace_root, task_id)
    if task is None:
        return None
    comments = load_task_thread(workspace_root, task)
    fresh = [
        c for c in comments
        if c.step == step
        and c.verdict in {"pass", "reject", "blocked"}
        and _parse_iso(c.created_at) > after_ts
    ]
    if not fresh:
        return None
    latest = fresh[-1]
    return AgentVerdict(
        outcome=latest.verdict,
        reason=latest.message or "",
        metadata={
            "files_changed": list(latest.files_changed),
        },
    )


def _parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class HeruEngineAdapter:
    """``Engine`` that delegates to ``SubagentManager`` for one turn."""

    def __init__(self, engine_name: str, workspace_root: Path) -> None:
        self.name = engine_name
        self.workspace_root = Path(workspace_root)

    def run_turn(self, session: Session, prompt: Any, state: TaskState) -> AgentVerdict:
        if not isinstance(prompt, dict):
            raise UnrecoverableError(
                f"HeruEngineAdapter expects a prompt dict from RoleAgent.build_prompt, got {type(prompt).__name__}"
            )

        task = get_task(self.workspace_root, state.task_id)
        if task is None:
            raise UnrecoverableError(f"task {state.task_id} not found in workspace")

        step = prompt["stage"]
        role = prompt["role"]
        prompt_text = serialize_prompt(prompt, task_record=task, workspace_root=self.workspace_root)

        before_turn = datetime.now(UTC)
        manager = SubagentManager(self.workspace_root)
        try:
            result = manager.run(
                task,
                role=role,
                engine_name=self.name,
                prompt=prompt_text,
                resume_session_id=session.engine_session_id,
            )
        except Exception as exc:
            self._reraise(exc)
            raise  # unreachable

        # Update the session with the new heru continuation id (if any).
        new_session_id = self._extract_continuation_id(result, session.engine_session_id)
        if new_session_id:
            session.engine_session_id = new_session_id
        session.turn_count = (session.turn_count or 0) + 1

        # Did the agent submit a verdict during this turn?
        verdict = _latest_verdict_after(self.workspace_root, state.task_id, step, before_turn)
        if verdict is None:
            raise NudgeRequired(
                f"{self.name} finished {step} without a litehive report submission"
            )

        # If this was a grooming verdict, the planner may have embedded a
        # TASK_UPDATE block (or text sections like ACCEPTANCE_CRITERIA /
        # PLAN / CONSTRAINTS) in its message. Apply those to the task
        # record so downstream stages see the updated intent.
        if step == "grooming" and task is not None:
            from .task_updates import apply_task_updates_from_comment

            message = verdict.reason or ""
            apply_task_updates_from_comment(self.workspace_root, task, message=message)

        return verdict

    @staticmethod
    def _extract_continuation_id(result, fallback: str | None) -> str | None:
        from litehive.agents._models import SubagentResult

        if not isinstance(result, SubagentResult):
            return fallback
        execution = result.execution
        if execution is None:
            return fallback
        if execution.continuation is None:
            return fallback
        return execution.continuation.resume_id or fallback

    @staticmethod
    def _reraise(exc: Exception) -> None:
        """Translate heru exceptions into the error taxonomy."""
        from heru import RetryableExecutionFailure

        kind = exc.kind if isinstance(exc, RetryableExecutionFailure) else None
        message = str(exc)

        if kind in {"quota_exhausted", "rate_limited"}:
            raise QuotaExceeded(message) from exc
        if kind in {"overloaded", "service_overloaded"}:
            raise EngineOverloaded(message) from exc
        if kind in {"timeout", "network", "transient"} or _is_retryable_failure(exc):
            raise TransientError(message) from exc

        # heru.EngineError and unknown exceptions: assume unrecoverable
        # so the state machine routes through recovery.
        raise UnrecoverableError(f"{type(exc).__name__}: {message}") from exc


def _is_retryable_failure(exc: Exception) -> bool:
    cls_name = type(exc).__name__
    return cls_name in {"RetryableExecutionFailure", "TimeoutError", "ConnectionError"}


def heru_engine_factory(workspace_root: Path):
    """Return a callable that produces ``HeruEngineAdapter`` instances.

    Suitable as the ``engine_factory`` argument for
    ``ConfigBackedEngineSelector``.
    """
    root = Path(workspace_root)

    def _factory(engine_name: str) -> Engine:
        return HeruEngineAdapter(engine_name, root)

    return _factory
