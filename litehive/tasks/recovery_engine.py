"""Engine selection for merge-resolution and recovery-style follow-ups."""

from litehive.config.model import LitehiveConfig
from litehive.git.ops import GitError
from litehive.domain.task import TaskRecord
from litehive.workspace import Workspace


def resolve_recovery_engine(
    workspace: Workspace,
    task: TaskRecord,
    config: LitehiveConfig | None,
) -> tuple[str, str | None]:
    """
    Pick the engine/model pair for a recovery or merge-resolution invocation.

    Honours the operator's ``recovery_engine`` override and refuses to fall
    through to a different engine when the chosen one is unavailable; called
    by the recovery and merge-resolving stages before they spawn a subagent
    so an unreachable engine fails loudly instead of silently swapping for
    a different model.
    """
    # inline: avoids a module-load cycle while callers migrate to the policy.
    from litehive.config.engine_models import EngineRoutingPolicy  # noqa: PLC0415

    if config is None:
        return "codex", None
    try:
        return EngineRoutingPolicy(workspace, config).resolve_recovery_engine(task)
    except RuntimeError as exc:
        raise GitError(str(exc)) from exc
