from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from litehive.config.model import LitehiveConfig
from litehive.config.profiles.model import ProcessProfile
from litehive.domain.common import PipelineState, TaskStage
from litehive.domain.roles import known_agent_role
from litehive.lifecycle.nodes.agent import AgentNode, EngineSelector, SessionProvider
from litehive.lifecycle.persistence import LastRejection, TaskState
from litehive.lifecycle.prompt_types import AgentPrompt
from litehive.workspace import Workspace
from .guidance import default_startup_guidance


@dataclass
class PromptContext:
    """Workspace-level context the runner provides to agents at construction.

    - ``workspace``: resolved workspace dependency supplied by the
      process-level container; used to read per-project overlays at
      ``.litehive/agents/{role}.md`` and ``.litehive/agents/all.md``.
    - ``config``: already-loaded workspace config. When unavailable
      (usually in tests), config-backed prompt sections are omitted
      instead of loading config inside the role.
    - ``startup_guidance``: extra bullets per role, merged on top of the
      built-in ``DEFAULT_STARTUP_GUIDANCE``. Usually comes from workspace
      config.
    - ``profile_overlay``: optional process-profile data (generic, codehive,
      django, …) that can add per-stage instruction blocks.
    """

    workspace: Workspace
    config: LitehiveConfig | None = None
    startup_guidance: dict[str, list[str]] = field(default_factory=dict)
    profile_overlay: ProcessProfile | None = None

    @property
    def workspace_root(self) -> Path:
        """Return the workspace root for path-only prompt helpers."""
        return self.workspace.root


def _bulletize(lines: list[str]) -> str:
    """
    Render a list of guidance lines as a Markdown bullet block.

    Centralised so every layer that emits "startup guidance"
    produces the same shape and the rendered prompt stays
    diff-friendly between runs — without one helper, the role and
    cross-role layers could format bullets differently and produce
    spurious diffs.
    """
    bullets: list[str] = []
    for line in lines:
        bullets.append(f"- {line}")
    return "\n".join(bullets)


def _stage_hook_summaries(raw_hooks: list) -> list[dict[str, Any]]:
    """
    Project after-stage hook config into a ``{command, description}`` summary list.

    Stringifies both fields so the agent prompt always shows
    well-typed values even if the operator's YAML supplied
    integers / ``None``. Caller:
    :meth:`RoleAgent._runner_hooks_for_stage`.
    """
    summaries: list[dict[str, Any]] = []
    for hook in raw_hooks:
        summaries.append({
            "command": str(hook.get("command", "")),
            "description": str(hook.get("description", "") or ""),
        })
    return summaries


class RoleAgent(AgentNode):
    """Base for stage-bound agents.

    Subclasses set up to five class attributes:

    - ``NODE_NAME`` — pipeline node the agent implements (``"grooming"``, …).
    - ``ROLE`` — logical agent role (``"planner"``, ``"swe"``, …).
    - ``ROLE_INSTRUCTIONS`` / ``INSTRUCTIONS`` — role-specific bullet block.
    - ``FRESH_ATTEMPT_INSTRUCTIONS`` — extra guidance when no current
      rejection applies to this role's prompt.
    - ``RETRY_ATTEMPT_INSTRUCTIONS`` — extra guidance when a current
      rejection does apply to this role's prompt.

    ``build_prompt`` composes instructions from six layers:

    1. Role-specific instructions.
    2. A selected fresh/retry attempt block, if defined.
    3. Built-in startup guidance for this role, optionally extended by
       workspace config.
    4. A per-workspace ``.litehive/agents/{role}.md`` override. If present
       it REPLACES layer 2 for that role.
    5. An ``"all"`` layer applied to every role, with the same md-override
       behavior.
    6. An optional process-profile overlay keyed by stage name.
    """

    # Subclasses override these. ``NODE_NAME``/``ROLE`` are required and
    # enforced in ``__init__``; the instruction blocks default to empty so
    # subclasses can declare only the layers they actually use.
    NODE_NAME: PipelineState
    ROLE: str
    ROLE_INSTRUCTIONS: str = ""
    INSTRUCTIONS: str = ""
    FRESH_ATTEMPT_INSTRUCTIONS: str = ""
    RETRY_ATTEMPT_INSTRUCTIONS: str = ""

    def __init__(
        self,
        selector: EngineSelector,
        session_provider: SessionProvider,
        prompt_context: PromptContext,
        retry_budget: int = 3,
        retry_on: tuple[str, ...] = ("execution_limit", "timeout"),
        retry_backoff_seconds: float = 0.0,
        retry_backoff_multiplier: float = 2.0,
        grace_period_seconds: int | None = None,
    ) -> None:
        """
        Verify the subclass declared its NODE_NAME/ROLE and forward
        the rest of the wiring to ``AgentNode``.

        ``prompt_context`` is captured on the instance so
        ``build_prompt`` can layer workspace overlays without
        re-reading config every turn — without that capture, every
        turn would re-resolve the workspace startup guidance.
        """
        if not self.NODE_NAME or not self.ROLE:
            raise TypeError(f"{type(self).__name__} must set NODE_NAME and ROLE class attributes")
        agent_role = known_agent_role(self.ROLE)
        if agent_role is not None and self.NODE_NAME not in agent_role.pipeline_stages:
            raise TypeError(f"{type(self).__name__} stage {self.NODE_NAME} does not match role {self.ROLE}")
        super().__init__(
            name=self.NODE_NAME,
            selector=selector,
            session_provider=session_provider,
            retry_budget=retry_budget,
            retry_on=retry_on,
            retry_backoff_seconds=retry_backoff_seconds,
            retry_backoff_multiplier=retry_backoff_multiplier,
            grace_period_seconds=grace_period_seconds,
        )
        self.prompt_context = prompt_context

    def build_prompt(self, state: TaskState) -> AgentPrompt:
        """Assemble the typed prompt payload every stage agent receives at turn start.

        Bundles the role/stage identity, retry counters, layered instruction
        text, last report, the rejection that should drive this turn, the
        failed-run history, and runner hooks. Called by the lifecycle runner
        (and overridden by stage-specific subclasses to add their extras).
        """
        last_rejection = self._last_rejection_for_prompt(state)
        instruction_layers, instruction_variant = self._assemble_instruction_layers(last_rejection)
        runner_hooks = self._runner_hooks_for_stage()
        return AgentPrompt(
            role=self.ROLE,
            stage=self.NODE_NAME,
            pipeline_mode=state.pipeline_mode,
            stage_retry=state.stage_retry.get(self.NODE_NAME, 0),
            instruction_variant=instruction_variant,
            instruction_layers=instruction_layers,
            last_report=state.last_report.to_payload(),
            last_rejection=_last_rejection_payload_or_none(last_rejection),
            failed_run_history=_failed_run_history_payload(state),
            runner_hooks=runner_hooks,
        )

    def _last_rejection_for_prompt(self, state: TaskState) -> LastRejection | None:
        """Return the rejection context the next agent turn should actually see.

        Implementing retries are special: work can be routed back there by
        rejects from implementing itself, testing, or accepting. The next SWE
        prompt should reflect the freshest reject that actually sent the task
        back to implementing, not whichever rejection was last stored under the
        implementing key.
        """
        fallback = state.last_rejection_by_stage.get(self.NODE_NAME)
        if self.NODE_NAME != PipelineState.IMPLEMENTING:
            return fallback
        rejection_stage = _latest_reject_stage_for_implementing(self.prompt_context.workspace, state.task_id)
        if rejection_stage is None:
            return fallback
        return state.last_rejection_by_stage.get(rejection_stage) or fallback

    def _runner_hooks_for_stage(self) -> list[dict[str, Any]]:
        """
        Return the configured after-stage hooks for this node.

        Loads from workspace config so the agent's prompt can list the
        automated checks that will run on its output; surfacing them
        lets the agent pre-check locally before submitting and avoid
        the hook-rejection retry cycle.
        """
        after_phase = f"after_{self.NODE_NAME}"
        config = self.prompt_context.config
        if config is None:
            return []
        raw_hooks = (config.runner_hooks or {}).get(after_phase, [])
        return _stage_hook_summaries(raw_hooks)

    def _assemble_instruction_layers(self, last_rejection: LastRejection | None) -> tuple[list[tuple[str, str]], str]:
        """Compose the ordered (label, text) instruction layers and the fresh/retry variant for this turn.

        Layer ordering and the md-overrides-startup precedence are the
        contract between built-in role guidance, workspace overlays, and
        process profiles — that contract is what this assembler enforces.
        """
        layers: list[tuple[str, str]] = []

        role_instructions = (self.ROLE_INSTRUCTIONS or self.INSTRUCTIONS).strip()
        if role_instructions:
            layers.append(("role", role_instructions))

        attempt_label, attempt_text, instruction_variant = self._attempt_instruction_layer(last_rejection)
        if attempt_text:
            layers.append((attempt_label, attempt_text))

        for key in ("all", self.ROLE):
            md = self._load_overlay_md(key)
            if md is not None:
                layers.append((f"{key}:md", md))
                continue
            bullets = self._startup_guidance_for(key)
            if bullets:
                layers.append((f"{key}:startup", _bulletize(bullets)))

        profile = self.prompt_context.profile_overlay
        if profile is not None:
            stage_bullets = profile.stage_instructions.get(self.NODE_NAME)
            if stage_bullets:
                layers.append(("profile", "\n".join(stage_bullets)))

        return layers, instruction_variant

    def _attempt_instruction_layer(self, last_rejection: LastRejection | None) -> tuple[str, str, str]:
        """Pick the fresh-attempt or retry-attempt instruction block based on whether a rejection drives this turn.

        Returns the (label, text, variant) triple consumed by
        ``_assemble_instruction_layers``; variant is also exposed in the
        prompt so the agent can see whether it is on a fresh or retry pass.
        """
        if last_rejection is not None:
            retry_text = self.RETRY_ATTEMPT_INSTRUCTIONS.strip()
            if retry_text:
                return "attempt:retry", retry_text, "retry"
            return "", "", "retry"

        fresh_text = self.FRESH_ATTEMPT_INSTRUCTIONS.strip()
        if fresh_text:
            return "attempt:fresh", fresh_text, "fresh"
        return "", "", "fresh"

    def _startup_guidance_for(self, key: str) -> list[str]:
        """Merge built-in startup guidance with workspace-config additions for the role or ``"all"`` key, in that order, so workspace bullets append to the defaults rather than replacing them."""
        merged = list(default_startup_guidance().get(key, []))
        merged.extend(self.prompt_context.startup_guidance.get(key, []))
        return merged

    def _load_overlay_md(self, key: str) -> str | None:
        """
        Read the per-workspace ``.litehive/agents/{key}.md`` overlay.

        When present, the file *fully replaces* startup guidance for
        that key — that's the contract operators rely on when they
        want to override built-in role guidance entirely instead of
        just appending to it.
        """
        root = self.prompt_context.workspace_root
        md_path = root / ".litehive" / "agents" / f"{key}.md"
        if not md_path.is_file():
            return None
        text = md_path.read_text(encoding="utf-8").strip()
        return text or None


def _last_rejection_payload_or_none(last_rejection: LastRejection | None) -> dict[str, str] | None:
    """Wrap ``_last_rejection_payload`` so callers can hand off ``None`` directly — lets ``build_prompt`` avoid an inline ternary at the call site."""
    if last_rejection is None:
        return None
    return _last_rejection_payload(last_rejection)


def _last_rejection_payload(last_rejection: LastRejection) -> dict[str, str]:
    """
    Project the persisted rejection down to the fields the agent
    prompt is allowed to see.

    Storage-only metadata (timestamps, internal ids) is dropped so
    the agent sees only source/reason/phase plus optional
    classification — leaking storage internals into the prompt
    surface is what makes prompts brittle to schema changes.
    """
    payload = {
        "source": last_rejection.source,
        "reason": last_rejection.reason,
        "raised_at_phase": last_rejection.raised_at_phase,
    }
    if last_rejection.classification is not None:
        payload["classification"] = last_rejection.classification
    return payload


def _failed_run_history_payload(state: TaskState) -> list[dict[str, Any]]:
    """Flatten the failed-run history map into a stably-ordered list for the agent prompt so retry decisions can use it as context — sorted on key so the same history renders identically across runs."""
    sorted_items = sorted(state.failed_run_history.items())
    payload: list[dict[str, Any]] = []
    for key, record in sorted_items:
        payload.append({
            "key": key,
            "stage": record.stage,
            "failure_shape": record.failure_shape,
            "count": record.count,
            "first_at": record.first_at,
            "latest_at": record.latest_at,
            "last_reason": record.last_reason,
            "source": record.source,
            "classification": record.classification,
            "retry_limit": record.retry_limit,
            "failed_reason": record.failed_reason,
            "operator_override_count": record.operator_override_count,
        })
    return payload


def _build_implementing_retry_origin_by_phase() -> dict[str, PipelineState]:
    """
    Enumerate every phase name that maps back to its retry-origin stage.

    Implementing-stage retries can be entered from the
    ``before_/<bare>/after_`` triplet of three stages
    (implementing, testing, accepting); this expander generates
    every combination so :func:`_latest_reject_stage_for_implementing`
    can reverse-lookup phase → stage in O(1) instead of parsing
    the prefix at the call site.
    """
    mapping: dict[str, PipelineState] = {}
    stages = (TaskStage.IMPLEMENTING, TaskStage.TESTING, TaskStage.ACCEPTING)
    prefixes = ("before_", "", "after_")
    for stage in stages:
        for prefix in prefixes:
            mapping[f"{prefix}{stage.value}"] = PipelineState(stage.value)
    return mapping


_IMPLEMENTING_RETRY_ORIGIN_BY_PHASE: dict[str, PipelineState] = _build_implementing_retry_origin_by_phase()


def _latest_reject_stage_for_implementing(workspace: Workspace, task_id: str) -> PipelineState | None:
    """Find which stage actually rejected the task and routed it back to implementing this turn.

    Implementing can be re-entered from implementing, testing, or accepting,
    so the SWE prompt needs the freshest reject across all three rather than
    whatever was last stored under the implementing key. Walks the pipeline
    transition journal in reverse and returns the origin stage of the most
    recent ``Reject`` event whose source stage is one of those three.
    """
    try:
        from litehive.lifecycle.journal import SqliteJournal  # noqa: PLC0415

        transitions = SqliteJournal(workspace).load_transitions(task_id)
    except Exception:
        return None

    for row in reversed(transitions):
        if row.get("event_type") != "Reject":
            continue
        stage = _IMPLEMENTING_RETRY_ORIGIN_BY_PHASE.get(str(row.get("from_stage") or ""))
        if stage is not None:
            return stage
    return None
