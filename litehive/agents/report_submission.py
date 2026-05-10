"""Agent report submission service."""

from dataclasses import dataclass
from typing import Callable

from litehive.domain.agent import SubagentId
from litehive.domain.common import Verdict
from litehive.domain.reports import TaskActivityEntry, classify_task_activity_verdict
from litehive.domain.roles import agent_activity_verdicts_for_role, agent_verdict_requires_target_stage
from litehive.domain.task import TaskRecord
from litehive.agents.session_store import subagent_artifacts
from litehive.state.records import WorkspaceTasks
from litehive.tasks.activity import task_activity_store_for_task
from litehive.workspace import Workspace


@dataclass(frozen=True)
class AgentReportSubmissionError(Exception):
    """
    Domain error raised when an agent report submission fails validation.

    Carries an ``unauthorized`` flag so the CLI layer can map it to the
    correct HTTP-style exit code instead of a generic error.
    """

    message: str
    """Human-readable description of what went wrong."""

    unauthorized: bool = False
    """True when the agent's role or verdict is not permitted."""

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class AgentReportIdentity:
    """
    Resolved identity tuple for an in-flight agent report.

    Produced by ``AgentReportSubmitter._resolve_identity`` after verifying
    that the environment variables match a persisted subagent session.
    """

    role: str
    """Agent role extracted from the subagent session record."""

    subagent_id: SubagentId
    """Canonical subagent identifier from the environment."""


@dataclass(frozen=True)
class AgentReportRequest:
    """
    Raw input from the ``litehive agent report`` CLI command.

    Fields map one-to-one to the CLI options. Optional string fields use
    ``None`` to mean "not supplied"; ``_normalized_optional`` later
    converts blank strings to ``None`` for consistent handling.
    """

    task_id: str
    """Target task the agent is reporting on."""

    verdict: Verdict
    """Agent's assessment of the current stage."""

    message: str
    """Free-text explanation of the verdict."""

    explicit_stage: str | None
    """Stage override supplied by ``--stage`` on the command line."""

    target_stage: str | None
    """Destination stage for recovery verdicts that resume or advance."""

    files_changed: list[str]
    """File paths the agent claims to have modified."""

    follow_up_task_id: str | None
    """Optional id of a downstream task created by this agent."""


@dataclass(frozen=True)
class AgentReportSubmission:
    """
    Successful result returned after an agent report is recorded.

    The submitter appends a ``TaskActivityEntry`` to the activity store and
    then returns this snapshot so the CLI can display what was persisted.
    """

    task_id: str
    """Task the report was recorded against."""

    stage: str
    """Resolved pipeline stage the report is attributed to."""

    verdict: Verdict
    """Agent verdict that was recorded."""

    role: str
    """Agent role that submitted the report."""

    source_subagent_id: SubagentId
    """Subagent session that originated the report."""

    verdict_classification: str | None
    """Domain classification derived from the role/verdict pair."""

    target_stage: str | None
    """Destination stage for recovery verdicts, if applicable."""

    follow_up_task_id: str | None
    """Downstream task id, if one was supplied and validated."""


@dataclass(frozen=True)
class AgentReportSubmitter:
    """
    Service that validates, authorizes, and records an agent report.

    The CLI layer constructs this with environment-derived identity
    fields, then calls ``submit`` with the parsed request. All
    validation failures raise ``AgentReportSubmissionError`` so the
    hidden agent commands stay thin.
    """

    workspace: Workspace
    """Workspace used for task lookups and activity persistence."""

    load_pipeline_stage: Callable[[str], str | None]
    """Callback returning the current pipeline stage for a task id."""

    env_role: str | None
    """Agent role from ``LITEHIVE_AGENT_ROLE``, used for identity checks."""

    env_subagent_id: SubagentId | None
    """Subagent id from ``LITEHIVE_SUBAGENT_ID``, required for session lookup."""

    env_stage: str | None
    """Stage fallback from ``LITEHIVE_STAGE`` when no explicit stage is given."""

    def submit(self, request: AgentReportRequest) -> AgentReportSubmission:
        """
        Validate, authorize, and persist an agent report.

        Resolves the calling agent's identity from environment and session
        state, checks the verdict against the role's allow-list, then
        appends a ``TaskActivityEntry`` and returns the submission result.
        """
        task = self._load_task(request.task_id)
        identity = self._resolve_identity(task)
        normalized_target_stage = _normalized_optional(request.target_stage)
        self._check_verdict(identity.role, request.verdict, normalized_target_stage)
        normalized_follow_up_task = self._resolve_follow_up_task(request.follow_up_task_id, task)

        pipeline_stage = self._load_pipeline_stage(request.task_id)
        actual_stage = self._resolve_stage(request.explicit_stage, task, pipeline_stage)
        verdict_classification = classify_task_activity_verdict(identity.role, request.verdict)
        entry = TaskActivityEntry(
            source="agent",
            role=identity.role,
            stage=actual_stage,
            target_stage=normalized_target_stage,
            verdict=request.verdict,
            verdict_classification=verdict_classification,
            message=request.message,
            files_changed=list(request.files_changed),
            source_subagent_id=identity.subagent_id,
            follow_up_task_id=normalized_follow_up_task,
        )
        task_activity_store_for_task(self.workspace, task).append(entry)
        return AgentReportSubmission(
            task_id=task.id,
            stage=actual_stage,
            verdict=request.verdict,
            role=identity.role,
            source_subagent_id=identity.subagent_id,
            verdict_classification=verdict_classification,
            target_stage=normalized_target_stage,
            follow_up_task_id=normalized_follow_up_task,
        )

    def _load_task(self, task_id: str) -> TaskRecord:
        """
        Fetch a task record or raise if it does not exist.

        The task must exist in the workspace so that downstream code can
        safely dereference its pipeline state and subagent list.
        """
        task = WorkspaceTasks(self.workspace).get_record(task_id)
        if task is None:
            raise AgentReportSubmissionError(f"task {task_id} not found")
        return task

    def _resolve_identity(self, task: TaskRecord) -> AgentReportIdentity:
        """
        Derive the agent's role and subagent id from session state.

        Loads the persisted session for the environment-subagent id and
        cross-checks it against ``LITEHIVE_AGENT_ROLE`` to detect
        mismatched or spoofed identity.
        """
        if self.env_subagent_id is None:
            raise AgentReportSubmissionError("LITEHIVE_SUBAGENT_ID not set")

        session = subagent_artifacts(self.workspace, task.id, self.env_subagent_id).load_session_record()
        if not session:
            raise AgentReportSubmissionError(f"subagent session {self.env_subagent_id} not found for task {task.id}")

        payload_id = session.subagent_id
        if payload_id is not None and payload_id != self.env_subagent_id:
            raise AgentReportSubmissionError(f"subagent session id mismatch for {self.env_subagent_id}")

        role = session.role
        if role is None:
            raise AgentReportSubmissionError(f"subagent session {self.env_subagent_id} has no role")

        if self.env_role and self.env_role != role:
            raise AgentReportSubmissionError("LITEHIVE_AGENT_ROLE does not match subagent session identity")

        return AgentReportIdentity(role=role, subagent_id=self.env_subagent_id)

    def _check_verdict(self, role: str, verdict: Verdict, target_stage: str | None) -> None:
        """
        Verify the verdict is permitted for the role and target stage.

        Recovery verdicts require a ``target_stage``; other verdicts must
        not carry one. Raises ``AgentReportSubmissionError`` with
        ``unauthorized=True`` when the role does not allow the verdict.
        """
        allowed = agent_activity_verdicts_for_role(role)
        if verdict not in allowed:
            raise AgentReportSubmissionError("agent verdict is not allowed for this role", unauthorized=True)
        if agent_verdict_requires_target_stage(role, verdict):
            if not target_stage:
                raise AgentReportSubmissionError(f"recovery verdict '{verdict}' requires --target-stage")
            return
        if target_stage:
            raise AgentReportSubmissionError("--target-stage is only valid with recovery resume/advance verdicts")

    def _resolve_follow_up_task(self, follow_up_task_id: str | None, task: TaskRecord) -> str | None:
        """
        Validate a follow-up task reference if one was supplied.

        Ensures the referenced task exists and is not the current task,
        preventing circular self-references in the activity log.
        """
        normalized_follow_up_task = _normalized_optional(follow_up_task_id)
        if normalized_follow_up_task is None:
            return None
        if normalized_follow_up_task == task.id:
            raise AgentReportSubmissionError("follow-up task id cannot reference the current task")
        if WorkspaceTasks(self.workspace).get_record(normalized_follow_up_task) is None:
            raise AgentReportSubmissionError(f"follow-up task {normalized_follow_up_task} not found")
        return normalized_follow_up_task

    def _load_pipeline_stage(self, task_id: str) -> str | None:
        """
        Delegate to the injected pipeline-stage loader callback.

        The callback is supplied by the caller because report submission
        does not own the pipeline state machine.
        """
        return self.load_pipeline_stage(task_id)

    def _resolve_stage(self, explicit_stage: str | None, task: TaskRecord, pipeline_stage: str | None) -> str:
        """
        Pick the effective stage from explicit, env, pipeline, and task defaults.

        Priority order: CLI ``--stage``, ``LITEHIVE_STAGE`` env variable,
        injected pipeline stage callback, task's current pipeline stage,
        and finally the task's pipeline status as a last resort.
        """
        if explicit_stage:
            return explicit_stage
        if self.env_stage:
            return self.env_stage
        if pipeline_stage:
            return pipeline_stage
        runtime_stage = task.current_pipeline_stage
        if runtime_stage:
            return runtime_stage
        return task.pipeline_status


def _normalized_optional(value: str | None) -> str | None:
    """
    Convert blank or whitespace-only strings to ``None``.

    CLI optional string fields arrive as empty strings when the user
    omits the option; normalizing them to ``None`` keeps downstream
    checks simple.
    """
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized
