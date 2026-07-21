"""Unit tests for services/llm_format.py — Anthropic <-> OpenAI translation.

These are the wire-format converters the daemon tool loop and the workflow
engine rely on to run non-Anthropic providers. Pure functions, no I/O.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from services.llm_format import (
    anthropic_messages_to_openai,  # noqa: E402
    anthropic_tools_to_openai,
)

pytestmark = pytest.mark.unit


class TestToolConversion:
    def test_input_schema_becomes_parameters(self):
        out = anthropic_tools_to_openai(
            [
                {
                    "name": "isolate_host",
                    "description": "d",
                    "input_schema": {"type": "object"},
                }
            ]
        )
        assert out == [
            {
                "type": "function",
                "function": {
                    "name": "isolate_host",
                    "description": "d",
                    "parameters": {"type": "object"},
                },
            }
        ]

    def test_already_openai_shape_passes_through(self):
        tool = {"type": "function", "function": {"name": "x", "parameters": {}}}
        assert anthropic_tools_to_openai([tool]) == [tool]

    def test_missing_schema_defaults(self):
        out = anthropic_tools_to_openai([{"name": "x"}])
        assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


class TestMessageConversion:
    def test_string_content_passes_through_unchanged(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert anthropic_messages_to_openai(msgs) == msgs

    def test_assistant_tool_use_becomes_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "let me check"},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "list_findings",
                        "input": {"limit": 5},
                    },
                ],
            }
        ]
        out = anthropic_messages_to_openai(msgs)
        assert len(out) == 1
        msg = out[0]
        assert msg["role"] == "assistant"
        assert msg["content"] == "let me check"
        assert len(msg["tool_calls"]) == 1
        tc = msg["tool_calls"][0]
        assert tc["id"] == "call_1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "list_findings"
        assert json.loads(tc["function"]["arguments"]) == {"limit": 5}

    def test_thinking_block_is_dropped(self):
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "secret"},
                    {"type": "text", "text": "answer"},
                ],
            }
        ]
        out = anthropic_messages_to_openai(msgs)
        assert out[0]["content"] == "answer"
        assert "tool_calls" not in out[0]

    def test_tool_result_becomes_tool_role_message(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": "42 findings",
                    }
                ],
            }
        ]
        out = anthropic_messages_to_openai(msgs)
        assert out == [
            {"role": "tool", "tool_call_id": "call_1", "content": "42 findings"}
        ]

    def test_tool_result_with_block_list_content_is_flattened(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "c1",
                        "content": [{"type": "text", "text": "line"}],
                    }
                ],
            }
        ]
        out = anthropic_messages_to_openai(msgs)
        assert out[0]["content"] == "line"

    def test_multi_turn_roundtrip_ordering(self):
        # assistant(tool_use) then user(tool_result) -> assistant(tool_calls),
        # tool message, in that order (OpenAI requires results right after call).
        msgs = [
            {"role": "user", "content": "investigate"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "c1", "name": "get_case", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": "case data"}
                ],
            },
        ]
        out = anthropic_messages_to_openai(msgs)
        assert [m["role"] for m in out] == ["user", "assistant", "tool"]
        assert out[1]["tool_calls"][0]["id"] == "c1"
        assert out[2]["tool_call_id"] == "c1"

    def test_synthesizes_id_when_tool_use_id_missing(self):
        msgs = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "x", "input": {}}],
            }
        ]
        out = anthropic_messages_to_openai(msgs)
        assert out[0]["tool_calls"][0]["id"]  # non-empty synthesized id
