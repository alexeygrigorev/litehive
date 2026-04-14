from litehive.cli.runner import _dry_run_stop_conditions
from litehive.config.model import LitehiveConfig


def test_dry_run_stop_conditions_apply_cli_overrides() -> None:
    config = LitehiveConfig(
        pool_stop_on_failure=False,
        pool_max_tasks=10,
        pool_stop_on_dirty_git=False,
        pool_stop_on_attention=True,
    )

    conditions = _dry_run_stop_conditions(
        config,
        stop_on_failure=True,
        max_tasks=3,
        stop_on_dirty_git=True,
    )

    assert conditions.stop_on_failure is True
    assert conditions.max_tasks == 3
    assert conditions.stop_on_dirty_git is True
    assert conditions.stop_on_attention is True
