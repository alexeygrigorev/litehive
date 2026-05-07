"""Typed prompt payload returned by :class:`RoleAgent.build_prompt`.

The role agents used to return ``dict[str, Any]`` here, which made it hard
to tell from the type alone what fields actually reach the engine. These
dataclasses promote that payload to a typed object so the serializer,
heru adapter, and recovery extension all see the same documented shape.

Two classes live here:

- :class:`AgentPrompt` — the base payload every stage agent assembles.
- :class:`RecoveryPrompt` — the recovery-only extension produced by
  :class:`RecoveryAgent`, carrying the extra diagnostic fields the
  recovery role's serializer sections need.

Both are frozen + slots: prompts are constructed once per turn and read
many times by the serializer; mutating them in-place would silently
diverge the rendered text from the persisted state. Use
``dataclasses.replace`` for the few places that need a tweaked variant
(e.g. the nudge prompt builder).
"""

from dataclasses import dataclass, field
from typing import Any

from litehive.domain.common import PipelineState

from .types import PipelineMode


@dataclass(frozen=True, slots=True)
class FailedSubagentDiagnostics:
    """
    Prompt-ready evidence for the failed subagent that triggered recovery.
    """

    subagent_id: str
    role: str | None
    engine: str | None
    status: str | None
    path: str
    exit_code: int | None
    did_produce_output: bool
    session_created_at: str | None
    session_updated_at: str | None
    report_summary: str
    transcript: str
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class AgentPrompt:
    """Structured payload every stage agent passes to the engine adapter.

    Each field is rendered by ``serialize_prompt`` (or a delegated section
    builder) into the engine-facing prompt text. The order of fields
    mirrors the order ``RoleAgent.build_prompt`` populates them.
    """

    role: str
    """Logical agent role (``"planner"``, ``"swe"``, ``"qa"``, ``"reviewer"``,
    ``"recovery"``, ``"merge-resolver"``). Drives verdict-vocabulary selection
    in the verdict-instructions section and labels the prompt header."""

    stage: PipelineState
    """Pipeline node the agent is running for. Surfaces in the header and
    is used by activity trimming so each stage sees only the prior context
    that matters to it."""

    pipeline_mode: PipelineMode
    """Whether the lifecycle is in ``full`` or ``single`` mode. Rendered in
    the header so the agent knows which contract is in effect for verdict
    routing."""

    stage_retry: int
    """Zero-based count of prior attempts at this stage. Drives the
    ``Stage retry attempt`` header line and gates the prior-work section
    that summarizes what the previous retry actually changed."""

    instruction_variant: str
    """Either ``"fresh"`` or ``"retry"`` — the attempt-flavor
    ``_attempt_instruction_layer`` selected. Tests assert on this so the
    agent knows whether it is on a fresh or retry pass."""

    instruction_layers: list[tuple[str, str]]
    """Ordered ``(label, text)`` pairs the instruction-section renderer
    walks: role guidance, attempt block, startup overlays, workspace
    overlays, and process profile. The label drives the heading text."""

    last_report: dict[str, Any]
    """The previous turn's persisted ``LastReport`` payload (changed files,
    test results, hook ok-ness, …). Carried as the dict shape produced by
    ``LastReport.to_payload`` so the serializer can read it through the
    same JSON-friendly accessors as the rest of the prompt."""

    last_rejection: dict[str, str] | None
    """Projection of the most recent ``LastRejection`` for the stage that
    drives this turn, or ``None`` on a fresh attempt. Stored as a dict
    payload (source/reason/raised_at_phase, optional classification) so
    the serializer's ``_last_rejection_section`` can render it directly
    and the activity trimmer can dedupe matching log entries."""

    failed_run_history: list[dict[str, Any]]
    """Stable-ordered flatten of ``state.failed_run_history`` so retry
    decisions can use cross-run failure-shape memory; rendered by
    ``_failed_run_history_section``."""

    runner_hooks: list[dict[str, Any]]
    """Configured ``after_<stage>`` runner hooks (``command`` +
    ``description`` pairs) so the agent knows which automated checks will
    gate its verdict and can pre-run them locally."""

    activity: list[dict[str, Any]] | None = None
    """Cross-stage activity log injected by the serializer (or by tests)
    after ``build_prompt`` returns. Trimmed by stage-specific rules
    before rendering so the agent only sees the prior verdicts that
    matter."""

    nudge: bool = False
    """``True`` when the agent finished a previous turn without submitting
    a verdict and is being re-prompted. ``build_nudge_prompt`` flips this
    on a copy of the original prompt."""

    nudge_message: str = ""
    """Human-readable reminder rendered in the nudge section when
    ``nudge`` is ``True``."""

    conflict_files: list[str] = field(default_factory=list)
    """Merge conflict file list populated by :class:`MergeAgent`; rendered
    by the merge-conflict serializer section so the resolver agent knows
    which files to fix."""

    merge_attempt: int | None = None
    """Counter of how many times merge resolution has been retried on
    this task. Set by :class:`MergeAgent` alongside ``conflict_files``."""


@dataclass(frozen=True, slots=True)
class RecoveryPrompt(AgentPrompt):
    """Recovery-only extension carrying the extra diagnostic fields the
    recovery role needs to fix Litehive infrastructure bugs without
    re-running the failed stage.

    All extra fields are optional because the recovery turn can be
    reached even when some evidence sources are missing (no failed
    subagent yet, no source-checkout configured, etc.)."""

    litehive_source_path: str | None = None
    """Raw configured ``litehive_source_path`` from the workspace config,
    or ``None`` when unset. Surfaced so the recovery agent can confirm
    the configured value matches the execution root it was handed."""

    recovery_execution_root: str | None = None
    """Directory the recovery agent should patch (the configured Litehive
    source checkout, falling back to the workspace root). Recovery exists
    to fix Litehive infrastructure bugs; without this redirect the agent
    edits the wrong tree."""

    recovery_config_diagnostic: dict[str, str] | None = None
    """When loading the workspace config to resolve
    ``litehive_source_path`` failed, this carries the failure kind and
    exception details so the agent can see why its source path is missing
    instead of silently defaulting to the task workspace."""

    recovery_trigger: dict[str, Any] | None = None
    """Serialized ``RecoveryTrigger`` describing the failure that sent
    the task into recovery (origin stage, trigger event kind, failure
    fingerprint, reason code, message)."""

    recovery_history: list[dict[str, Any]] = field(default_factory=list)
    """Merged runtime + ``TaskState`` history of prior recovery attempts
    on this task. Helps the agent spot loops instead of re-trying the
    same fix."""

    repeated_recovery_fingerprint: dict[str, Any] | None = None
    """Set when the same origin/fingerprint already appears in
    ``recovery_history``; the recovery role guidance forces escalation
    (file a follow-up task, reject) rather than re-routing the task."""

    recovery_failure_explanation: str | None = None
    """Free-form explanation attached to the active recovery turn, taken
    from ``TaskState.recovery_failure_explanation`` and surfaced under
    the recovery-trigger section."""

    failed_subagent_diagnostics: FailedSubagentDiagnostics | None = None
    """Persisted evidence of the failed subagent run (session, report,
    transcript, stdout/stderr, exit code) so the recovery agent does not
    have to dig for it."""

    scope_analysis: dict[str, Any] | None = None
    """Worktree-scope-vs-task-scope classification telling the recovery
    agent whether deleted files look like operator cleanup or SWE scope
    creep."""

    test_failure_attribution: dict[str, Any] | None = None
    """When recovery is triggered by a test failure, the attribution
    payload lets the agent see whether failing tests touch this task's
    surface or look pre-existing."""
