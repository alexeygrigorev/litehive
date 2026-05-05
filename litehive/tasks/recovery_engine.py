"""Engine selection for merge-resolution and recovery-style follow-ups."""

from pathlib import Path

from litehive.config.model import LitehiveConfig
from litehive.git.ops import GitError
from litehive.domain.task import TaskRecord


def resolve_recovery_engine(
    root: Path,
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
    # inline: kept so tests can monkey-patch ``select_engine`` on the
    # config.engine_models module (the canonical home) and have callers
    # here see it.
    from litehive.config.engine_models import select_engine  # noqa: PLC0415

    if config is None:
        return "codex", None

    engine_override = None
    if config.recovery_engine and config.recovery_engine != "auto":
        engine_override = config.recovery_engine

    selection = select_engine(
        root,
        task,
        config,
        engine_override=engine_override,
        require_available=True,
    )
    if selection.engine_name is None:
        raise GitError(selection.blocked_reason or "no eligible recovery engine available")
    return selection.engine_name, selection.model_name
