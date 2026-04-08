"""Tests that engine adapters log warnings when skipping unparseable output."""

import json
import logging

from litehive.engines.base import iter_jsonl_payloads
from litehive.engines.adapters.claude import _extract_claude_text_delta_fallback
from litehive.engines.adapters.goz import _goz_extract_text
from litehive.engines.adapters.codex import _extract_codex_transcript
from litehive.engines.adapters.opencode import _extract_opencode_transcript


# --- iter_jsonl_payloads (base.py) ---


def test_iter_jsonl_warns_on_invalid_json(caplog):
    with caplog.at_level(logging.WARNING, logger="litehive.engines.base"):
        result = iter_jsonl_payloads("not json\n{}\n")
    assert len(result) == 1
    assert any("unparseable JSON" in m for m in caplog.messages)


def test_iter_jsonl_warns_on_non_dict(caplog):
    with caplog.at_level(logging.WARNING, logger="litehive.engines.base"):
        result = iter_jsonl_payloads('[1,2,3]\n{"ok":true}\n')
    assert len(result) == 1
    assert any("expected dict" in m for m in caplog.messages)


def test_iter_jsonl_no_warning_on_blank_lines(caplog):
    with caplog.at_level(logging.WARNING, logger="litehive.engines.base"):
        iter_jsonl_payloads('{"a":1}\n\n{"b":2}\n')
    assert not caplog.messages


# --- claude delta fallback ---


def test_claude_delta_warns_on_decode_failure(caplog):
    # Line matches the regex but has an invalid JSON string escape
    bad_line = '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"\\xinvalid"}}'
    with caplog.at_level(logging.WARNING, logger="litehive.engines.adapters.claude"):
        _extract_claude_text_delta_fallback(bad_line)
    assert any("decode failed" in m for m in caplog.messages)


def test_claude_delta_no_warning_on_valid(caplog):
    line = '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}'
    with caplog.at_level(logging.WARNING, logger="litehive.engines.adapters.claude"):
        result = _extract_claude_text_delta_fallback(line)
    assert result == ["hello"]
    assert not caplog.messages


# --- goz extract_text ---


def test_goz_warns_on_unrecognized_keys(caplog):
    with caplog.at_level(logging.WARNING, logger="litehive.engines.adapters.goz"):
        result = _goz_extract_text({"unknown_key": "value"})
    assert result is None
    assert any("no recognized text key" in m for m in caplog.messages)


def test_goz_no_warning_on_known_key(caplog):
    with caplog.at_level(logging.WARNING, logger="litehive.engines.adapters.goz"):
        result = _goz_extract_text({"text": "hello"})
    assert result == "hello"
    assert not caplog.messages


def test_goz_no_warning_on_empty_dict(caplog):
    with caplog.at_level(logging.WARNING, logger="litehive.engines.adapters.goz"):
        result = _goz_extract_text({})
    assert result is None
    assert not caplog.messages


# --- codex transcript ---


def test_codex_warns_on_non_dict_item(caplog):
    payload = json.dumps({"type": "item.completed", "item": "not_a_dict"})
    with caplog.at_level(logging.WARNING, logger="litehive.engines.adapters.codex"):
        _extract_codex_transcript(payload)
    assert any("non-dict item" in m for m in caplog.messages)


def test_codex_warns_on_missing_text(caplog):
    payload = json.dumps({"type": "item.completed", "item": {"type": "agent_message"}})
    with caplog.at_level(logging.WARNING, logger="litehive.engines.adapters.codex"):
        _extract_codex_transcript(payload)
    assert any("missing text" in m for m in caplog.messages)


# --- opencode transcript ---


def test_opencode_warns_on_non_dict_part(caplog):
    payload = json.dumps({"type": "text", "part": "not_a_dict"})
    with caplog.at_level(logging.WARNING, logger="litehive.engines.adapters.opencode"):
        _extract_opencode_transcript(payload)
    assert any("non-dict part" in m for m in caplog.messages)


def test_opencode_warns_on_missing_text(caplog):
    payload = json.dumps({"type": "text", "part": {"no_text": True}})
    with caplog.at_level(logging.WARNING, logger="litehive.engines.adapters.opencode"):
        _extract_opencode_transcript(payload)
    assert any("missing text" in m for m in caplog.messages)
