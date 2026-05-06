"""HeruEngineFactory — produces ``Engine`` instances backed by heru.

The factory takes a workspace root and returns a callable
``Callable[[str], Engine]`` suitable for the ``ConfigBackedEngineSelector``.
Each call to the inner callable produces a fresh ``HeruEngineAdapter`` for
the requested engine name.

The adapter delegates the actual subagent invocation to ``SubagentManager``
(``litehive.agents.SubagentManager``) so we don't reimplement heru's CLI
shelling, execution-trace handling, or session management. We sit on top of it,
translating to/from the pipeline contract:

  - prompt dict → serialized string via ``serialize_prompt``
  - SubagentResult → ``AgentVerdict`` via the verdict reader, which checks
    whether a fresh ``litehive agent report`` submission landed in the
    workspace journal during this turn
  - heru exceptions → error taxonomy

Boundary:

  - ``litehive.roles`` owns prompt policy and role guidance.
  - ``litehive.agents`` owns process execution, sandboxing, sessions, and
    artifacts.
  - this module owns lifecycle engine construction and the adapter that turns a
    Heru-backed subagent run into lifecycle ``Engine`` outcomes.
"""

from dataclasses import replace
from datetime import UTC, datetime
import logging
from pathlib import Path
import re
from typing import Any

from heru.adapters import CodexCLIAdapter
from litehive.agents.manager import SubagentManager, SubagentStartupError
from litehive.container import build_container, build_subagent_manager, build_workspace
from litehive.domain.agent import EngineFailure
from litehive.domain.common import OutcomeReasonCode, PipelineState, TaskStage, cap_feedback
from litehive.domain.reports import StageReport, TaskActivityStage, canonical_report_pipeline_state
from litehive.domain.lifecycle_deltas import recovery_trigger_from_event
from litehive.git.ops import GitError, is_git_repo, status_porcelain
from litehive.roles.base import PromptContext
from litehive.roles.recovery import RecoveryAgent
from litehive.state.records import get_task, get_task_worktree_path
from litehive.tasks.activity import latest_task_activity_entry, load_task_activity, save_task_activity
from litehive.tasks.journal import append_journal
from litehive.tasks.activity_rendering import normalized_files_changed
from litehive.tasks.report_storage import rewrite_latest_stage_report
from litehive.workspace import Workspace
from litehive.worktree.paths import resolve_recorded_worktree_path

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
from .prompt_types import AgentPrompt
from .sessions import Session


class _MissingActivityEntry(Exception):
    """Internal: agent finished without producing a fresh activity entry."""


logger = logging.getLogger(__name__)


class _NullSelector:
    """
    Stub engine selector for the direct-recovery prompt build.

    Constructed only when ``RecoveryAgent`` is being instantiated to
    render its prompt during the direct-recovery handoff; no engine
    pick happens because the handoff shells Codex itself rather than
    going through the normal selector path.
    """

    def select(self, state, node_name, excluded):
        """
        Always return ``None``.

        The direct-recovery handoff only needs ``RecoveryAgent`` to
        render its prompt — the actual engine call happens inside the
        adapter, so no selection is required.
        """
        del state, node_name, excluded
        return None


class _NullSessions:
    """
    Stub session store paired with ``_NullSelector``.

    Used only by the direct-recovery prompt build: that turn bypasses
    ``SubagentManager`` entirely, so no continuation IDs are produced
    or persisted and a real session store would only see throwaway
    rows.
    """

    def get_or_create(self, task_id, node_name, engine_name):
        """
        Return a throwaway empty ``Session``.

        The direct-recovery turn shells Codex itself instead of going
        through SubagentManager, so no real session ever exists; the
        caller only needs *something* shaped like a Session to satisfy
        ``RecoveryAgent.build_prompt``.
        """
        del task_id, node_name, engine_name
        return Session()

    def persist(self, task_id, node_name, engine_name, session):
        """
        Swallow the persist call.

        The direct-recovery bypass produces no continuation we want to
        remember, so writing it would just leave a phantom row behind
        on the next normal launch.
        """
        del task_id, node_name, engine_name, session


def _allowed_verdicts_for_stage(stage: TaskActivityStage) -> set[str]:
    """Verdict vocabularies the journal reader accepts when scanning for the
    agent's submission. Recovery has its own routing verbs (resume/advance/...);
    every other stage only emits pass/reject."""
    if stage == PipelineState.RECOVERING:
        return {"resume", "advance", "done", "budget_hit", "reject"}
    return {"pass", "reject"}


def _execution_checkout_path(workspace_root: Path, task) -> Path:
    """Resolve the working directory the subagent should run in for a task —
    the per-task worktree if one was recorded, otherwise the workspace root.
    Falls back to the workspace so engines never see a missing cwd."""
    return (
        resolve_recorded_worktree_path(
            workspace_root,
            get_task_worktree_path(task),
        )
        or workspace_root
    )


def _recovery_execution_root(workspace_root: Path) -> Path:
    """Recovery agents edit litehive's own source tree, not the user's task
    worktree. Resolve ``litehive_source_path`` from config and run the recovery
    turn there; fall back to the workspace if the source path is unset or
    unreadable."""
    try:
        config = build_container(workspace_root).config
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
    if resolved.is_dir():
        return resolved
    return workspace_root


def _agent_execution_root(workspace_root: Path, task, role: str) -> Path:
    """Pick the cwd for the subagent based on role: recovery agents fix
    litehive itself (source tree), every other role works inside the task's
    worktree."""
    if role == "recovery":
        return _recovery_execution_root(workspace_root)
    return _execution_checkout_path(workspace_root, task)


def execution_checkout_status(workspace_root: Path, task) -> tuple[Path, list[str] | None]:
    """Return the task's execution checkout and its ``git status --porcelain``
    lines. Used by the implementing-pass guard to decide whether the SWE
    actually edited files; ``None`` means no git repo or git refused to answer
    and the caller should not flag a hallucination on that basis."""
    checkout = _execution_checkout_path(workspace_root, task)
    if not is_git_repo(checkout):
        return checkout, None
    try:
        return checkout, status_porcelain(checkout)
    except GitError:
        return checkout, None


def _display_path(root: Path, path: Path) -> str:
    """Render a path for human-facing journal/report text relative to the
    workspace, so messages don't leak the operator's home directory."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return str(path)
    if str(relative) == "":
        return "."
    return str(relative)


def _rewrite_hallucinated_implementing_pass(
    workspace: Workspace,
    task,
    latest,
    claimed_files: list[str],
    checkout: Path,
) -> AgentVerdict:
    """Retract a SWE's implementing ``pass`` when the worktree is clean but
    the agent claimed file edits. Rewrites the activity entry, replaces the
    stage report with a reject, and journals the hallucination so downstream
    routing treats it as a real reject. Called by the implementing-pass guard
    inside ``latest_verdict_after``."""
    checkout_display = _display_path(workspace.root, checkout)
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

    activity_entries = load_task_activity(workspace, task)
    for entry in reversed(activity_entries):
        if entry.created_at != latest.created_at:
            continue
        if entry.role != latest.role or entry.stage != latest.stage:
            continue
        if entry.message != latest.message or list(entry.files_changed) != list(latest.files_changed):
            continue
        entry.verdict = "reject"
        if "[retracted - filesystem check shows no changes landed]" not in entry.message:
            entry.message = f"{entry.message.rstrip()}\n[retracted - filesystem check shows no changes landed]"
        entry.message = f"{entry.message.rstrip()}\n{detail}"
        break
    save_task_activity(workspace, task, activity_entries)

    report = StageReport(
        task_id=task.id,
        pipeline_state=TaskStage.IMPLEMENTING,
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
    report_path = rewrite_latest_stage_report(workspace, task, report)
    append_journal(
        workspace,
        task,
        (
            "Rejected implementing pass as hallucinated completion.\n"
            f"reason_code: `{reason_code}`\n"
            f"`git status --porcelain` in `{checkout_display}` returned no changes, but the SWE claimed: {claimed}\n"
            f"report: `{report_path.relative_to(workspace.root)}`"
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
    """Mine the agent's free-form report message for short test/lint result
    lines (``pytest``, ``ruff``, ``mypy`` evidence) so the next stage's prompt
    can echo concrete verification signals back to the next agent without
    re-running the suite."""
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
    stage: TaskActivityStage,
    after_ts: datetime,
    source_subagent_id: str | None = None,
) -> AgentVerdict | None:
    """Return the most recent activity entry for ``(task_id, stage)`` whose
    ``created_at`` is newer than ``after_ts``, mapped to an ``AgentVerdict``.

    Returns ``None`` when nothing newer landed — caller raises ``NudgeRequired``.
    """
    task = get_task(workspace_root, task_id)
    if task is None:
        return None
    workspace = build_workspace(workspace_root)
    latest = latest_task_activity_entry(
        workspace,
        task,
        stage=stage,
        source_subagent_id=source_subagent_id,
        verdicts=_allowed_verdicts_for_stage(stage),
        after=after_ts,
    )
    if latest is None:
        return None
    changed_files = normalized_files_changed(latest.files_changed)
    if stage == TaskStage.IMPLEMENTING and latest.verdict == "pass":
        checkout, worktree_status = execution_checkout_status(workspace_root, task)
        if worktree_status == [] and changed_files:
            return _rewrite_hallucinated_implementing_pass(
                workspace,
                task,
                latest=latest,
                claimed_files=changed_files,
                checkout=checkout,
            )
    metadata: dict[str, Any] = {
        "files_changed": changed_files,
        "target_stage": latest.target_stage,
        "last_report": {
            "changed_files": changed_files,
            "test_results": _extract_test_results(latest.message or ""),
        },
    }
    if latest.verdict == "reject":
        classification = latest.verdict_classification
    else:
        classification = None
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
        workspace: Workspace,
        model_name: str | None = None,
    ) -> None:
        """
        Pin a heru-backed engine to a workspace and optional model.

        Constructed by ``heru_engine_factory`` per selector pick so
        multiple selectors using the same engine name don't share
        mutable adapter state — each pick gets its own instance.
        """
        self.name = engine_name
        self.workspace_root = Path(workspace_root)
        self.workspace = workspace
        self.model_name = model_name

    def with_model(self, model_name: str | None) -> "HeruEngineAdapter":
        """
        Return a sibling adapter pinned to a specific model.

        Used when the selector wants to retry the same engine on a
        different model without mutating the existing instance, so the
        original adapter (used by other in-flight stages) is not
        disturbed.
        """
        return HeruEngineAdapter(
            self.name,
            self.workspace_root,
            workspace=self.workspace,
            model_name=model_name,
        )

    def run_turn(self, session: Session, prompt: Any, state: TaskState) -> AgentVerdict:
        """
        Run one agent turn for the lifecycle pipeline.

        Serializes the role's prompt, hands it to ``SubagentManager``,
        then reads the journal for the verdict the agent submitted via
        ``litehive agent report``. Raises ``NudgeRequired`` if the
        agent finished without submitting, and translates engine
        failures into the ``Engine`` error taxonomy that AgentNode
        knows how to react to.
        """
        if not isinstance(prompt, AgentPrompt):
            raise UnrecoverableError(
                f"HeruEngineAdapter expects an AgentPrompt from RoleAgent.build_prompt, got {type(prompt).__name__}"
            )

        task = get_task(self.workspace_root, state.task_id)
        if task is None:
            raise UnrecoverableError(f"task {state.task_id} not found in workspace")

        stage = prompt.stage
        report_stage = canonical_report_pipeline_state(stage.value)
        role = prompt.role
        prompt_text = serialize_prompt(prompt, task_record=task, workspace_root=self.workspace_root)
        execution_root = _agent_execution_root(self.workspace_root, task, role=role)

        before_turn = datetime.now(UTC)
        try:
            manager = build_subagent_manager(
                self.workspace_root,
                execution_root=execution_root,
                manager_cls=SubagentManager,
            )
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

        # Did the agent submit a verdict during this turn?
        verdict = latest_verdict_after(
            self.workspace_root,
            state.task_id,
            report_stage,
            before_turn,
            source_subagent_id=result.ref.id,
        )
        if verdict is None:
            raise NudgeRequired(f"{self.name} finished {stage} without a litehive agent report submission")

        return verdict

    def _run_with_crash_resume(
        self,
        manager: SubagentManager,
        task,
        role: str,
        prompt_text: str,
        session: Session,
    ):
        """Drive ``SubagentManager.run`` with a single crash-resume retry: if
        the engine exits non-zero but left a continuation handle, we resume
        that session once with the crash-resume preamble before giving up.
        Keeps ``session.engine_session_id`` updated so same-engine nudges and
        retries continue the same conversation."""
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
        """
        Prepend the crash-resume preamble to the original prompt.

        Used by ``_run_with_crash_resume`` so a resumed session sees an
        explicit ``continue where you left off`` instruction before the
        role's prompt body — without it, the resumed engine would re-
        read the original instructions and likely repeat work it
        already finished.
        """
        return f"{cls.CRASH_RESUME_PROMPT_PREFIX}{prompt_text}"

    def _handle_startup_failure(
        self,
        state: TaskState,
        task,
        role: str,
        startup_message: str,
        original_exc: Exception,
    ) -> AgentVerdict:
        """``SubagentManager`` itself failed to launch — usually because the
        litehive install is broken. Try a direct Codex shell as the recovery
        agent so the system can self-heal; if we're not in recovery (or the
        bypass produced no verdict), re-raise the original exception so the
        state machine routes through normal recovery."""
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
        state: TaskState,
        task,
        startup_message: str,
    ) -> AgentVerdict | None:
        """Last-resort path when SubagentManager won't start: build the
        recovery prompt, shell Codex directly against litehive's source tree,
        and read the journal for whatever verdict the recovery agent
        submitted. Returns ``None`` if the task isn't actually in recovery so
        the caller falls back to the original exception."""
        recovery_prompt = self._direct_recovery_prompt(task=task, state=state, startup_message=startup_message)
        recovery_execution_root = _agent_execution_root(self.workspace_root, task, role="recovery")
        after_ts = datetime.min.replace(tzinfo=UTC)
        if state.stage == PipelineState.RECOVERING:
            previous_recovery = latest_task_activity_entry(
                self.workspace,
                task,
                stage=PipelineState.RECOVERING,
                verdicts=_allowed_verdicts_for_stage(PipelineState.RECOVERING),
            )
            if previous_recovery is not None:
                created_at = previous_recovery.created_at
                if isinstance(created_at, datetime):
                    after_ts = created_at
                else:
                    after_ts = datetime.fromisoformat(str(created_at))
        source_subagent_id = "direct-recovery"
        self._run_direct_recovery_turn(
            task_id=state.task_id,
            execution_root=recovery_execution_root,
            prompt_text=recovery_prompt,
            source_subagent_id=source_subagent_id,
        )
        if state.stage != PipelineState.RECOVERING:
            return None
        return latest_verdict_after(
            self.workspace_root,
            state.task_id,
            PipelineState.RECOVERING,
            after_ts,
            source_subagent_id=source_subagent_id,
        )

    def _direct_recovery_prompt(self, task, state: TaskState, startup_message: str) -> str:
        """Render the recovery role's prompt for the direct-Codex bypass,
        synthesizing a recovery trigger from the startup failure so the
        agent sees the same prompt shape it would normally receive."""
        recovery_state = self._direct_recovery_state(state, startup_message)
        recovery_agent = RecoveryAgent(
            _NullSelector(),
            _NullSessions(),
            prompt_context=PromptContext(workspace_root=self.workspace_root),
        )
        prompt = recovery_agent.build_prompt(recovery_state)
        return serialize_prompt(prompt, task_record=task, workspace_root=self.workspace_root)

    def _direct_recovery_state(self, state: TaskState, startup_message: str) -> TaskState:
        """Project the live ``TaskState`` into a recovering state so the
        ``RecoveryAgent`` prompt builder sees a valid trigger and explanation
        even when the original failure happened before any state machine
        transition into recovery."""
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
            stage=PipelineState.RECOVERING,
            active_recovery_trigger=trigger,
            recovery_failure_explanation=self._direct_recovery_explanation(
                state.recovery_failure_explanation,
                startup_message,
            ),
        )

    @staticmethod
    def _direct_recovery_explanation(existing: str | None, startup_message: str) -> str:
        """Compose the operator-visible reason that this turn bypassed
        SubagentManager. Appends the bypass note to any existing explanation
        so we keep prior recovery context, and dedupes if the note is already
        present from an earlier bypass."""
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
        task_id: str,
        execution_root: Path,
        prompt_text: str,
        source_subagent_id: str,
    ):
        """Shell Codex directly against litehive's own source tree, registering
        a synthetic subagent record so the journal entry the recovery agent
        submits can be attributed back to this bypass turn."""
        from litehive.agents.session_store import save_subagent_artifacts  # noqa: PLC0415

        save_subagent_artifacts(
            self.workspace,
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
                "LITEHIVE_STAGE": PipelineState.RECOVERING.value,
            },
        )

    @staticmethod
    def extract_continuation_id(result, fallback: str | None) -> str | None:
        """Pull the engine's resume ID out of a ``SubagentResult`` so the next
        turn (nudge, retry, or crash-resume) continues the same conversation.
        Falls back to ``fallback`` when the result didn't include a fresh
        handle so we don't drop the previously-recorded session."""
        from litehive.domain.agent import SubagentResult  # noqa: PLC0415

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
        from heru import RetryableExecutionFailure  # noqa: PLC0415

        if isinstance(exc, RetryableExecutionFailure):
            kind: str | None = exc.classification
        else:
            kind = None
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
        """Translate a structured ``EngineFailure`` (already classified by the
        subagent layer) into the ``Engine`` error taxonomy the lifecycle
        runner reacts to."""
        if failure.kind == "execution_limit":
            raise TransientError(failure.reason, failure_kind="execution_limit")
        if failure.kind == "retryable_execution_error":
            raise TransientError(
                failure.reason,
                failure_kind=failure.classification or "service",
            )
        raise UnrecoverableError(f"{failure.kind}: {failure.reason}")


def _is_retryable_failure(exc: Exception) -> bool:
    """Name-based fallback used by ``_reraise`` when heru's own ``kind`` field
    is missing — mirrors the transient classes we already know to retry so
    timeouts and connection drops don't escalate to ``UnrecoverableError``."""
    cls_name = type(exc).__name__
    return cls_name in {"RetryableExecutionFailure", "TimeoutError", "ConnectionError"}


def heru_engine_factory(workspace_root: Path):
    """Return a callable that produces ``HeruEngineAdapter`` instances. Suitable as the ``engine_factory`` argument for ``ConfigBackedEngineSelector``."""
    root = Path(workspace_root)
    workspace = build_workspace(root)

    def _factory(engine_name: str) -> Engine:
        return HeruEngineAdapter(engine_name, root, workspace=workspace)

    return _factory
