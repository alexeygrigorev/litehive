import logging

from litehive.agents.unified_events import parse_unified_execution


def test_parse_unified_execution_logs_invalid_payload(caplog) -> None:
    stdout = '{"kind":"message","content":123}\n'

    with caplog.at_level(logging.WARNING, logger="heru.unified_events"):
        parsed = parse_unified_execution(stdout)

    assert parsed is None
    assert "Discarding invalid unified event payload" in caplog.text
