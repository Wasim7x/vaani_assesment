"""Tests for the LLM client's low-level parsers. Nothing hits the
network. The streaming path has an integration test that runs only if
--live-groq is passed; we skip it by default."""
from __future__ import annotations

from relay.llm_client import _parse_sse_chunk, _parse_retry_after


def test_parse_sse_chunk_content_delta():
    line = 'data: {"choices":[{"delta":{"content":"he"}}]}'
    c = _parse_sse_chunk(line)
    assert c is not None
    assert c["choices"][0]["delta"]["content"] == "he"


def test_parse_sse_chunk_done_marker():
    assert _parse_sse_chunk("data: [DONE]") is None


def test_parse_sse_chunk_ignores_bad_json():
    assert _parse_sse_chunk("data: not-json") is None


def test_parse_sse_chunk_ignores_non_data_lines():
    assert _parse_sse_chunk(": heartbeat") is None
    assert _parse_sse_chunk("") is None


def test_parse_retry_after_number():
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("2.5") == 2.5


def test_parse_retry_after_handles_none_and_garbage():
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("Thu, 01 Jan 1970") is None
