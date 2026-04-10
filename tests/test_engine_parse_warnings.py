"""Tests for engine output parse failure warnings (T-0201)."""

import json
import logging

from litehive.agents.base import extract_codex_messages, iter_jsonl_payloads


def test_iter_jsonl_payloads_warns_on_invalid_json(caplog):
    """iter_jsonl_payloads logs a warning for lines that are not valid JSON."""
    stdout = '{"type": "ok"}\nNOT_JSON_LINE\n{"type": "also_ok"}\n'
    with caplog.at_level(logging.WARNING, logger="litehive.agents.base"):
        payloads = iter_jsonl_payloads(stdout)
    assert len(payloads) == 2
    assert any("unparseable line 2" in rec.message for rec in caplog.records)


def test_iter_jsonl_payloads_warns_on_non_dict_json(caplog):
    """iter_jsonl_payloads logs a warning for lines that are valid JSON but not objects."""
    stdout = '{"type": "ok"}\n[1, 2, 3]\n"just a string"\n42\n'
    with caplog.at_level(logging.WARNING, logger="litehive.agents.base"):
        payloads = iter_jsonl_payloads(stdout)
    assert len(payloads) == 1
    assert sum("non-object JSON" in rec.message for rec in caplog.records) == 3


def test_iter_jsonl_payloads_no_warning_on_blank_lines(caplog):
    """Blank lines should be silently skipped without warnings."""
    stdout = '{"type": "ok"}\n\n  \n{"type": "also_ok"}\n'
    with caplog.at_level(logging.WARNING, logger="litehive.agents.base"):
        payloads = iter_jsonl_payloads(stdout)
    assert len(payloads) == 2
    assert not caplog.records


def test_iter_jsonl_payloads_coalesces_repeated_multiline_codex_command_warnings(caplog):
    """Malformed multiline command_execution payloads should warn once and keep valid events."""
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "msg_1", "type": "agent_message", "text": "before"},
                }
            ),
            '{"type":"item.completed","item":{"id":"cmd_1","type":"command_execution","command":"python -c \\"print(1)',
            'print(2)\\"","aggregated_output":"line 1',
            'line 2","exit_code":1,"status":"failed"}}',
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "msg_2", "type": "agent_message", "text": "after"},
                }
            ),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="litehive.agents.base"):
        first_pass = iter_jsonl_payloads(stdout)
        second_pass = iter_jsonl_payloads(stdout)
        transcript = extract_codex_messages(stdout)

    assert first_pass == second_pass
    assert [payload["type"] for payload in first_pass] == ["item.completed", "item.completed"]
    assert transcript == "before\nafter"
    warnings = [rec.message for rec in caplog.records if "unparseable" in rec.message]
    assert len(warnings) == 1
    assert "skipping 3 consecutive unparseable lines starting at 2" in warnings[0]
    assert '"type":"command_execution"' in warnings[0]


def test_claude_delta_fallback_warns_on_decode_failure(caplog):
    """Claude delta fallback logs a warning when JSON string literal decode fails."""
    from litehive.agents.adapters.claude import _extract_claude_text_delta_fallback

    # Craft a line that matches the regex but has an invalid JSON string literal
    bad_line = '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"\\uZZZZ"}}'
    stdout = bad_line + "\n"
    with caplog.at_level(logging.WARNING, logger="litehive.agents.adapters.claude"):
        result = _extract_claude_text_delta_fallback(stdout)
    assert result == []
    assert any("claude delta fallback" in rec.message and "line 1" in rec.message for rec in caplog.records)


def test_claude_delta_fallback_no_warning_on_valid(caplog):
    """No warning for valid delta lines."""
    from litehive.agents.adapters.claude import _extract_claude_text_delta_fallback

    good_line = '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}'
    stdout = good_line + "\n"
    with caplog.at_level(logging.WARNING, logger="litehive.agents.adapters.claude"):
        result = _extract_claude_text_delta_fallback(stdout)
    assert result == ["hello"]
    assert not caplog.records


def test_goz_extract_text_warns_on_unrecognized_dict(caplog):
    """goz _goz_extract_text logs a warning when a dict has no recognized keys."""
    from litehive.agents.adapters.goz import _goz_extract_text

    unrecognized = {"unknown_key": "some value", "another": 42}
    with caplog.at_level(logging.WARNING, logger="litehive.agents.adapters.goz"):
        result = _goz_extract_text(unrecognized)
    assert result is None
    assert any("_goz_extract_text could not extract text" in rec.message for rec in caplog.records)


def test_goz_extract_text_no_warning_on_known_keys(caplog):
    """No warning when dict has recognized text key."""
    from litehive.agents.adapters.goz import _goz_extract_text

    with caplog.at_level(logging.WARNING, logger="litehive.agents.adapters.goz"):
        result = _goz_extract_text({"text": "hello"})
    assert result == "hello"
    assert not caplog.records


def test_goz_extract_text_no_warning_on_empty_dict(caplog):
    """No warning for empty dict (nothing to extract)."""
    from litehive.agents.adapters.goz import _goz_extract_text

    with caplog.at_level(logging.WARNING, logger="litehive.agents.adapters.goz"):
        result = _goz_extract_text({})
    assert result is None
    assert not caplog.records


def test_codex_transcript_warns_on_non_dict_item(caplog):
    """codex _extract_codex_transcript warns when item.completed has non-dict item."""
    from litehive.agents.adapters.codex import _extract_codex_transcript

    stdout = json.dumps({"type": "item.completed", "item": "not_a_dict"}) + "\n"
    with caplog.at_level(logging.WARNING, logger="litehive.agents.adapters.codex"):
        result = _extract_codex_transcript(stdout)
    assert result == ""
    assert any("non-dict item" in rec.message for rec in caplog.records)


def test_codex_transcript_warns_on_missing_text(caplog):
    """codex _extract_codex_transcript warns when agent_message has no text."""
    from litehive.agents.adapters.codex import _extract_codex_transcript

    stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}) + "\n"
    with caplog.at_level(logging.WARNING, logger="litehive.agents.adapters.codex"):
        result = _extract_codex_transcript(stdout)
    assert result == ""
    assert any("no text field" in rec.message for rec in caplog.records)


def test_opencode_transcript_warns_on_non_dict_part(caplog):
    """opencode _extract_opencode_transcript warns when text event has non-dict part."""
    from litehive.agents.adapters.opencode import _extract_opencode_transcript

    stdout = json.dumps({"type": "text", "part": "not_a_dict"}) + "\n"
    with caplog.at_level(logging.WARNING, logger="litehive.agents.adapters.opencode"):
        result = _extract_opencode_transcript(stdout)
    assert result == ""
    assert any("non-dict part" in rec.message for rec in caplog.records)


def test_opencode_transcript_warns_on_missing_text(caplog):
    """opencode _extract_opencode_transcript warns when part has no text field."""
    from litehive.agents.adapters.opencode import _extract_opencode_transcript

    stdout = json.dumps({"type": "text", "part": {"image": "data"}}) + "\n"
    with caplog.at_level(logging.WARNING, logger="litehive.agents.adapters.opencode"):
        result = _extract_opencode_transcript(stdout)
    assert result == ""
    assert any("no text field" in rec.message for rec in caplog.records)
