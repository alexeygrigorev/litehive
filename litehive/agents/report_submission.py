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
    message: str
    unauthorized: bool = False

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class AgentReportIdentity:
    role: str
    subagent_id: SubagentId


@dataclass(frozen=True)
class AgentReportRequest:
    task_id: str
    verdict: Verdict
    message: str
    explicit_stage: str | None
    target_stage: str | None
    files_changed: list[str]
    follow_up_task_id: str | None


@dataclass(frozen=True)
class AgentReportSubmission:
    task_id: str
    stage: str
    verdict: Verdict
    role: str
    source_subagent_id: SubagentId
    verdict_classification: str | None
    target_stage: str | None
    follow_up_task_id: str | None


@dataclass(frozen=True)
class AgentReportSubmitter:
    workspace: Workspace
    load_pipeline_stage: Callable[[str], str | None]
    env_role: str | None
    env_subagent_id: SubagentId | None
    env_stage: str | None

    def submit(self, request: AgentReportRequest) -> AgentReportSubmission:
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
        task = WorkspaceTasks(self.workspace).get_record(task_id)
        if task is None:
            raise AgentReportSubmissionError(f"task {task_id} not found")
        return task

    def _resolve_identity(self, task: TaskRecord) -> AgentReportIdentity:
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
        normalized_follow_up_task = _normalized_optional(follow_up_task_id)
        if normalized_follow_up_task is None:
            return None
        if normalized_follow_up_task == task.id:
            raise AgentReportSubmissionError("follow-up task id cannot reference the current task")
        if WorkspaceTasks(self.workspace).get_record(normalized_follow_up_task) is None:
            raise AgentReportSubmissionError(f"follow-up task {normalized_follow_up_task} not found")
        return normalized_follow_up_task

    def _load_pipeline_stage(self, task_id: str) -> str | None:
        return self.load_pipeline_stage(task_id)

    def _resolve_stage(self, explicit_stage: str | None, task: TaskRecord, pipeline_stage: str | None) -> str:
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
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized
