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
    whether a fresh ``litehive report`` submission landed in the
    workspace journal during this turn
  - heru exceptions → error taxonomy
"""

from datetime import UTC, datetime
from pathlib import Path
import re
from typing import Any

from litehive.agents.manager import SubagentManager
from litehive.domain.agent import EngineFailure
from litehive.git.ops import GitError, current_head, is_git_repo, status_porcelain
from litehive.heru_compat import resolve_engine_resume_session_id
from litehive.state.records import get_task
from litehive.tasks.activity import latest_task_activity_entry
from litehive.tasks.reports import normalized_files_changed
from litehive.tasks.worktrees import resolve_recorded_worktree_path

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


def _allowed_verdicts_for_stage(stage: str) -> set[str]:
    if stage == "recovering":
        return {"resume", "advance", "done", "budget_hit", "reject"}
    return {"pass", "reject"}


def _execution_checkout_has_changes(workspace_root: Path, task_id: str) -> bool:
    task = get_task(workspace_root, task_id)
    if task is None:
        return False
    checkout = resolve_recorded_worktree_path(workspace_root, task.runtime.git.worktree_path) or workspace_root
    if not is_git_repo(checkout):
        return False
    try:
        if status_porcelain(checkout):
            return True
        workspace_head = current_head(workspace_root)
        checkout_head = current_head(checkout)
    except GitError:
        return False
    if workspace_head is None or checkout_head is None:
        return False
    return workspace_head != checkout_head


_REPORT_RESULT_HINTS = (
    "pytest",
    "ruff",
    "mypy",
    "test",
    "tests",
    "passed",
    "failed",
    "error",
    "ok",
    "verification",
    "verified",
    "status --full",
)


def _extract_test_results(message: str) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for raw_line in message.splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+\.)\s*", "", raw_line).strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith(("files changed:", "changed files:", "files:")):
            continue
        if not any(hint in lower for hint in _REPORT_RESULT_HINTS):
            continue
        if line in seen:
            continue
        seen.add(line)
        results.append(line)
    return results[:4]


def _latest_verdict_after(
    workspace_root: Path,
    task_id: str,
    stage: str,
    after_ts: datetime,
) -> AgentVerdict | None:
    """Return the most recent thread comment for ``(task_id, stage)`` whose
    ``created_at`` is newer than ``after_ts``, mapped to an ``AgentVerdict``.

    Returns ``None`` when nothing newer landed — caller raises ``NudgeRequired``.
    """
    task = get_task(workspace_root, task_id)
    if task is None:
        return None
    latest = latest_task_activity_entry(
        workspace_root,
        task,
        stage=stage,
        verdicts=_allowed_verdicts_for_stage(stage),
        after=after_ts,
    )
    if latest is None:
        return None
    if (
        stage == "implementing"
        and latest.verdict == "pass"
        and not _execution_checkout_has_changes(workspace_root, task_id)
    ):
        return AgentVerdict(
            outcome="reject",
            reason=(
                "implementing pass rejected: execution checkout is clean and HEAD matches the "
                "workspace base, so no work landed"
            ),
        )
    changed_files = normalized_files_changed(latest.files_changed)
    return AgentVerdict(
        outcome=latest.verdict,
        reason=latest.message or "",
        metadata={
            "files_changed": changed_files,
            "target_stage": latest.target_stage,
            "last_report": {
                "changed_files": changed_files,
                "test_results": _extract_test_results(latest.message or ""),
            },
        },
    )


class HeruEngineAdapter:
    """``Engine`` that delegates to ``SubagentManager`` for one turn."""

    _CRASH_RESUME_PROMPT_PREFIX = "Please continue where you left off. Complete the task.\n\n"

    def __init__(
        self,
        engine_name: str,
        workspace_root: Path,
        *,
        model_name: str | None = None,
    ) -> None:
        self.name = engine_name
        self.workspace_root = Path(workspace_root)
        self.model_name = model_name

    def with_model(self, model_name: str | None) -> "HeruEngineAdapter":
        return HeruEngineAdapter(
            self.name,
            self.workspace_root,
            model_name=model_name,
        )

    def run_turn(self, session: Session, prompt: Any, state: TaskState) -> AgentVerdict:
        if not isinstance(prompt, dict):
            raise UnrecoverableError(
                f"HeruEngineAdapter expects a prompt dict from RoleAgent.build_prompt, got {type(prompt).__name__}"
            )

        task = get_task(self.workspace_root, state.task_id)
        if task is None:
            raise UnrecoverableError(f"task {state.task_id} not found in workspace")

        stage = prompt["stage"]
        role = prompt["role"]
        prompt_text = serialize_prompt(prompt, task_record=task, workspace_root=self.workspace_root)
        execution_root = (
            resolve_recorded_worktree_path(self.workspace_root, task.runtime.git.worktree_path) or self.workspace_root
        )

        before_turn = datetime.now(UTC)
        manager = SubagentManager(self.workspace_root, execution_root=execution_root)
        result = self._run_with_crash_resume(
            manager,
            task,
            role=role,
            prompt_text=prompt_text,
            session=session,
        )

        if result.failure is not None:
            self._reraise_failure(result.failure)

        session.turn_count = (session.turn_count or 0) + 1

        # Did the agent submit a verdict during this turn?
        verdict = _latest_verdict_after(self.workspace_root, state.task_id, stage, before_turn)
        if verdict is None:
            raise NudgeRequired(f"{self.name} finished {stage} without a litehive report submission")

        return verdict

    def _run_with_crash_resume(
        self,
        manager: SubagentManager,
        task,
        *,
        role: str,
        prompt_text: str,
        session: Session,
    ):
        current_prompt = prompt_text
        resume_session_id = session.engine_session_id
        crash_resume_attempted = False

        while True:
            try:
                result = manager.run(
                    task,
                    role=role,
                    engine_name=self.name,
                    prompt=current_prompt,
                    model=self.model_name,
                    resume_session_id=resume_session_id,
                )
            except Exception as exc:
                self._reraise(exc)
                raise  # unreachable

            # Persist the latest continuation handle even if the attempt failed;
            # same-engine retries and nudges reuse the in-memory session object.
            new_session_id = self._extract_continuation_id(result, session.engine_session_id)
            if new_session_id:
                session.engine_session_id = new_session_id

            if result.failure is not None or result.exit_code == 0 or crash_resume_attempted:
                return result

            crash_resume_id = self._extract_continuation_id(result, None)
            if not crash_resume_id:
                return result

            crash_resume_attempted = True
            resume_session_id = resolve_engine_resume_session_id(self.name, crash_resume_id)
            current_prompt = self._crash_resume_prompt(prompt_text)

    @classmethod
    def _crash_resume_prompt(cls, prompt_text: str) -> str:
        return f"{cls._CRASH_RESUME_PROMPT_PREFIX}{prompt_text}"

    @staticmethod
    def _extract_continuation_id(result, fallback: str | None) -> str | None:
        from litehive.domain.agent import SubagentResult

        if not isinstance(result, SubagentResult):
            return fallback
        continuation = result.continuation
        if continuation is not None:
            return continuation.resume_id or fallback
        execution = result.execution
        if execution is None:
            return fallback
        continuation = getattr(execution, "continuation", None)
        if continuation is None:
            return fallback
        return continuation.resume_id or fallback

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

    @staticmethod
    def _reraise_failure(failure: EngineFailure) -> None:
        if failure.kind == "execution_limit":
            raise TransientError(failure.reason, failure_kind="execution_limit")
        if failure.kind == "retryable_execution_error":
            raise TransientError(
                failure.reason,
                failure_kind=failure.classification or "service",
            )
        raise UnrecoverableError(f"{failure.kind}: {failure.reason}")


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
