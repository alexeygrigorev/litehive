from litehive.domain.common import PipelineState, PipelineStatus, task_stage_for_pipeline_state


def test_pipeline_state_is_canonical_machine_state_not_pipeline_status_alias() -> None:
    assert PipelineState is not PipelineStatus
    assert PipelineState.AFTER_IMPLEMENTING == "after_implementing"
    assert "after_implementing" not in {status.value for status in PipelineStatus}


def test_pipeline_state_projection_to_user_facing_stage_is_explicit() -> None:
    assert task_stage_for_pipeline_state(PipelineState.AFTER_IMPLEMENTING) == "implementing"
