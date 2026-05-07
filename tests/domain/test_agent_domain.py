from litehive.domain.agent import ExecutionTrace


def test_execution_trace_preserves_action_chunks_and_rendered_text() -> None:
    trace = ExecutionTrace.from_text("step 1\n\nstep 2\n\n")

    assert trace.chunks == ("step 1", "step 2")
    assert trace.text == "step 1\n\nstep 2"


def test_empty_execution_trace_has_no_chunks_or_rendered_text() -> None:
    trace = ExecutionTrace.from_text("")

    assert trace.chunks == ()
    assert trace.text == ""
    assert not trace
