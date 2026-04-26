"""HeruEngineFactory — produces ``Engine`` instances backed by heru.

The factory takes a workspace root and returns a callable
``Callable[[str], Engine]`` suitable for the ``ConfigBackedEngineSelector``.
Each call to the inner callable produces a fresh ``HeruEngineAdapter`` for
the requested engine name.

The adapter delegates the actual subagent invocation to ``SubagentManager``
(``litehive.agents.SubagentManager``) so we don't reimplement heru's CLI
shelling, execution-trace handling, or session management. We sit on top of it,
translating to/from the v2 contract:

  - prompt dict → serialized string via ``serialize_prompt``
  - SubagentResult → ``AgentVerdict`` via the verdict reader, which checks
    whether a fresh ``litehive report`` submission landed in the
    workspace journal during this turn
  - heru exceptions → error taxonomy
"""

from dataclasses import replace
from datetime import UTC, datetime
import logging
from pathlib import Path
import re
from typing import Any

from heru.adapters import CodexCLIAdapter
from litehive.agents.manager import SubagentManager, SubagentStartupError
from litehive.config.loading import load_config
from litehive.domain.agent import EngineFailure
from litehive.domain.common import OutcomeReasonCode, Verdict, cap_feedback
from litehive.domain.reports import StageReport
from litehive.domain.lifecycle_deltas import recovery_trigger_from_event
from litehive.git.ops import GitError, is_git_repo, status_porcelain
from litehive.roles.base import PromptContext
from litehive.roles.recovery import RecoveryAgent
from litehive.state.records import get_task, get_task_worktree_path
from litehive.tasks.activity import latest_task_activity_entry, load_task_activity, save_task_activity
from litehive.tasks.journal import append_journal
from litehive.tasks.reports import normalized_files_changed, rewrite_latest_stage_report
from litehive.worktree import resolve_recorded_worktree_path

from .events import Crash
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


class _MissingActivityEntry(Exception):
    """Internal: agent finished without producing a fresh activity entry."""


logger = logging.getLogger(__name__)


class _NullSelector:
    def select(self, state, node_name, excluded):
        del state, node_name, excluded
        return None


class _NullSessions:
    def get_or_create(self, task_id, node_name, engine_name):
        del task_id, node_name, engine_name
        return Session()

    def persist(self, task_id, node_name, engine_name, session):
        del task_id, node_name, engine_name, session


def _allowed_verdicts_for_stage(stage: str) -> set[str]:
    if stage == "recovering":
        return {"resume", "advance", "done", "budget_hit", "reject"}
    return {"pass", "reject"}


def _execution_checkout_path(workspace_root: Path, task) -> Path:
    return (
        resolve_recorded_worktree_path(
            workspace_root,
            get_task_worktree_path(task),
        )
        or workspace_root
    )


def _recovery_execution_root(workspace_root: Path) -> Path:
    try:
        config = load_config(workspace_root)
    except Exception:
        return workspace_root
    raw_source = str(config.litehive_source_path or "").strip()
    if not raw_source:
        return workspace_root
    candidate = Path(raw_source).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    return resolved if resolved.is_dir() else workspace_root


def _agent_execution_root(workspace_root: Path, task, *, role: str) -> Path:
    if role == "recovery":
        return _recovery_execution_root(workspace_root)
    return _execution_checkout_path(workspace_root, task)


def execution_checkout_status(workspace_root: Path, task) -> tuple[Path, list[str] | None]:
    checkout = _execution_checkout_path(workspace_root, task)
    if not is_git_repo(checkout):
        return checkout, None
    try:
        return checkout, status_porcelain(checkout)
    except GitError:
        return checkout, None


def _display_path(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return str(path)
    return "." if str(relative) == "" else str(relative)


def _rewrite_hallucinated_implementing_pass(
    workspace_root: Path,
    task,
    *,
    latest,
    claimed_files: list[str],
    checkout: Path,
) -> AgentVerdict:
    checkout_display = _display_path(workspace_root, checkout)
    claimed = ", ".join(claimed_files)
    reason_code = OutcomeReasonCode.HALLUCINATED_COMPLETION.value
    reason = (
        "implementing pass rejected: `git status --porcelain` in the execution checkout was clean, "
        f"but the SWE reported changed files: {claimed}"
    )
    detail = (
        f"{reason}\n"
        f"reason_code: {reason_code}\n"
        f"execution_checkout: {checkout_display}\n"
        "git_status_porcelain: clean\n"
        f"claimed_files_changed: {claimed}"
    )

    activity_entries = load_task_activity(workspace_root, task)
    for entry in reversed(activity_entries):
        if entry.created_at != latest.created_at:
            continue
        if entry.role != latest.role or entry.stage != latest.stage:
            continue
        if entry.message != latest.message or list(entry.files_changed) != list(latest.files_changed):
            continue
        entry.verdict = Verdict.REJECT
        if "[retracted - filesystem check shows no changes landed]" not in entry.message:
            entry.message = f"{entry.message.rstrip()}\n[retracted - filesystem check shows no changes landed]"
        entry.message = f"{entry.message.rstrip()}\n{detail}"
        break
    save_task_activity(workspace_root, task, activity_entries)

    report = StageReport(
        task_id=task.id,
        pipeline_state="implementing",
        verdict="reject",
        source="agent",
        summary="implementing reject: pass report claimed files_changed but the execution checkout was clean",
        feedback=cap_feedback(detail),
        submitted_via_cli=False,
        failure_classification=reason_code,
        outcome_reason_code=OutcomeReasonCode.HALLUCINATED_COMPLETION,
        failure_diagnostics={
            "reason_code": reason_code,
            "execution_checkout": checkout_display,
            "git_status_porcelain": [],
            "claimed_files_changed": claimed_files,
        },
    )
    report_path = rewrite_latest_stage_report(workspace_root, task, report)
    append_journal(
        workspace_root,
        task,
        (
            "Rejected implementing pass as hallucinated completion.\n"
            f"reason_code: `{reason_code}`\n"
            f"`git status --porcelain` in `{checkout_display}` returned no changes, but the SWE claimed: {claimed}\n"
            f"report: `{report_path.relative_to(workspace_root)}`"
        ),
    )
    return AgentVerdict(
        outcome="reject",
        reason=reason,
        metadata={
            "reason_code": reason_code,
            "execution_checkout": checkout_display,
            "git_status_porcelain": [],
            "claimed_files_changed": claimed_files,
        },
        source="guard",
    )


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


def latest_verdict_after(
    workspace_root: Path,
    task_id: str,
    stage: str,
    after_ts: datetime,
    *,
    source_subagent_id: str | None = None,
) -> AgentVerdict | None:
    """Return the most recent activity entry for ``(task_id, stage)`` whose
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
        source_subagent_id=source_subagent_id,
        verdicts=_allowed_verdicts_for_stage(stage),
        after=after_ts,
    )
    if latest is None:
        return None
    changed_files = normalized_files_changed(latest.files_changed)
    if stage == "implementing" and latest.verdict == "pass":
        checkout, worktree_status = execution_checkout_status(workspace_root, task)
        if worktree_status == [] and changed_files:
            return _rewrite_hallucinated_implementing_pass(
                workspace_root,
                task,
                latest=latest,
                claimed_files=changed_files,
                checkout=checkout,
            )
    metadata = {
        "files_changed": changed_files,
        "target_stage": latest.target_stage,
        "last_report": {
            "changed_files": changed_files,
            "test_results": _extract_test_results(latest.message or ""),
        },
    }
    classification = latest.verdict_classification if latest.verdict == "reject" else None
    if classification:
        metadata["verdict_classification"] = classification
    return AgentVerdict(
        outcome=latest.verdict,
        reason=latest.message or "",
        classification=classification,
        metadata=metadata,
    )


class HeruEngineAdapter:
    """``Engine`` that delegates to ``SubagentManager`` for one turn."""

    CRASH_RESUME_PROMPT_PREFIX = "Please continue where you left off. Complete the task.\n\n"

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
        execution_root = _agent_execution_root(self.workspace_root, task, role=role)

        before_turn = datetime.now(UTC)
        try:
            manager = SubagentManager(self.workspace_root, execution_root=execution_root)
        except Exception as exc:
            return self._handle_startup_failure(
                state=state,
                task=task,
                role=role,
                startup_message=f"{type(exc).__name__}: {exc}",
                original_exc=exc,
            )

        try:
            result = self._run_with_crash_resume(
                manager,
                task,
                role=role,
                prompt_text=prompt_text,
                session=session,
            )
        except SubagentStartupError as exc:
            return self._handle_startup_failure(
                state=state,
                task=task,
                role=role,
                startup_message=exc.startup_message,
                original_exc=exc.original,
            )

        if result.failure is not None:
            self._reraise_failure(result.failure)

        session.turn_count = (session.turn_count or 0) + 1

        # Did the agent submit a verdict during this turn?
        verdict = latest_verdict_after(
            self.workspace_root,
            state.task_id,
            stage,
            before_turn,
            source_subagent_id=result.ref.id,
        )
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
            except SubagentStartupError:
                raise
            except Exception as exc:
                self._reraise(exc)
                raise  # unreachable

            # Persist the latest continuation handle even if the attempt failed;
            # same-engine retries and nudges reuse the in-memory session object.
            new_session_id = self.extract_continuation_id(result, session.engine_session_id)
            if new_session_id:
                session.engine_session_id = new_session_id

            if result.failure is not None or result.exit_code == 0 or crash_resume_attempted:
                return result

            crash_resume_id = self.extract_continuation_id(result, None)
            if not crash_resume_id:
                return result

            crash_resume_attempted = True
            resume_session_id = crash_resume_id
            current_prompt = self._crash_resume_prompt(prompt_text)

    @classmethod
    def _crash_resume_prompt(cls, prompt_text: str) -> str:
        return f"{cls.CRASH_RESUME_PROMPT_PREFIX}{prompt_text}"

    def _handle_startup_failure(
        self,
        *,
        state: TaskState,
        task,
        role: str,
        startup_message: str,
        original_exc: Exception,
    ) -> AgentVerdict:
        try:
            recovery_verdict = self._attempt_direct_recovery_handoff(
                state=state,
                task=task,
                startup_message=startup_message,
            )
        except Exception:
            logger.exception("Direct recovery handoff failed after subagent startup failure")
            recovery_verdict = None

        if role == "recovery" and recovery_verdict is not None:
            return recovery_verdict
        self._reraise(original_exc)
        raise AssertionError("unreachable")

    def _attempt_direct_recovery_handoff(
        self,
        *,
        state: TaskState,
        task,
        startup_message: str,
    ) -> AgentVerdict | None:
        recovery_prompt = self._direct_recovery_prompt(task=task, state=state, startup_message=startup_message)
        recovery_execution_root = _agent_execution_root(self.workspace_root, task, role="recovery")
        after_ts = datetime.min.replace(tzinfo=UTC)
        if state.stage == "recovering":
            previous_recovery = latest_task_activity_entry(
                self.workspace_root,
                task,
                stage="recovering",
                verdicts=_allowed_verdicts_for_stage("recovering"),
            )
            if previous_recovery is not None:
                after_ts = previous_recovery.created_at
        source_subagent_id = "direct-recovery"
        self._run_direct_recovery_turn(
            task_id=state.task_id,
            execution_root=recovery_execution_root,
            prompt_text=recovery_prompt,
            source_subagent_id=source_subagent_id,
        )
        if state.stage != "recovering":
            return None
        return latest_verdict_after(
            self.workspace_root,
            state.task_id,
            "recovering",
            after_ts,
            source_subagent_id=source_subagent_id,
        )

    def _direct_recovery_prompt(self, *, task, state: TaskState, startup_message: str) -> str:
        recovery_state = self._direct_recovery_state(state, startup_message)
        recovery_agent = RecoveryAgent(
            _NullSelector(),
            _NullSessions(),
            prompt_context=PromptContext(workspace_root=self.workspace_root),
        )
        prompt = recovery_agent.build_prompt(recovery_state)
        return serialize_prompt(prompt, task_record=task, workspace_root=self.workspace_root)

    def _direct_recovery_state(self, state: TaskState, startup_message: str) -> TaskState:
        trigger = state.active_recovery_trigger
        if trigger is None:
            trigger = recovery_trigger_from_event(
                state,
                Crash(
                    exc_type="SubagentStartupError",
                    message=startup_message,
                ),
            )
        return replace(
            state,
            stage="recovering",
            active_recovery_trigger=trigger,
            recovery_failure_explanation=self._direct_recovery_explanation(
                state.recovery_failure_explanation,
                startup_message,
            ),
        )

    @staticmethod
    def _direct_recovery_explanation(existing: str | None, startup_message: str) -> str:
        handoff = (
            "Litehive cannot start its own subagents for this task, so this recovery turn bypassed "
            f"SubagentManager and launched Codex directly. Startup failure: {startup_message}"
        )
        if not existing:
            return handoff
        if handoff in existing:
            return existing
        return f"{existing} {handoff}"

    def _run_direct_recovery_turn(
        self,
        *,
        task_id: str,
        execution_root: Path,
        prompt_text: str,
        source_subagent_id: str,
    ):
        from litehive.agents.session_store import save_subagent_artifacts

        save_subagent_artifacts(
            self.workspace_root,
            task_id,
            source_subagent_id,
            session={
                "id": source_subagent_id,
                "role": "recovery",
                "engine": "codex",
                "status": "running",
            },
        )
        adapter = CodexCLIAdapter()
        return adapter.run(
            prompt_text,
            cwd=execution_root,
            extra_env={
                "LITEHIVE_TASK_ID": task_id,
                "LITEHIVE_WORKSPACE_ROOT": str(self.workspace_root),
                "LITEHIVE_AGENT_ROLE": "recovery",
                "LITEHIVE_SUBAGENT_ID": source_subagent_id,
                "LITEHIVE_STAGE": "recovering",
            },
        )

    @staticmethod
    def extract_continuation_id(result, fallback: str | None) -> str | None:
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
