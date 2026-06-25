"""Unit tests for services.llm_worker.

Covers ``_adapt_router_result_to_raw`` — its ``stop_reason`` must reflect
whether the router returned tool_calls, otherwise AgentRunner's tool-use
loop drops every tool invocation from router-dispatched providers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from services.llm_worker import _adapt_router_result_to_raw

pytestmark = pytest.mark.unit


def test_adapt_router_result_emits_tool_use_stop_reason_when_tool_calls_present():
    router_result = {
        "content": "I'll list findings now.",
        "tool_calls": [
            {"id": "tool_1", "name": "list_findings", "input": {"limit": 5}}
        ],
        "input_tokens": 100,
        "output_tokens": 20,
        "provider": "anthropic",
        "path": "anthropic-direct",
    }

    adapted = _adapt_router_result_to_raw(router_result)

    assert adapted["stop_reason"] == "tool_use"
    tool_blocks = [b for b in adapted["content"] if b["type"] == "tool_use"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0]["name"] == "list_findings"
    assert tool_blocks[0]["input"] == {"limit": 5}


def test_adapt_router_result_emits_end_turn_when_no_tool_calls():
    router_result = {
        "content": "Investigation complete.",
        "tool_calls": None,
        "input_tokens": 50,
        "output_tokens": 10,
    }

    adapted = _adapt_router_result_to_raw(router_result)

    assert adapted["stop_reason"] == "end_turn"
    assert all(b["type"] != "tool_use" for b in adapted["content"])


def test_adapt_router_result_emits_end_turn_when_tool_calls_empty_list():
    router_result = {
        "content": "Nothing to do.",
        "tool_calls": [],
        "input_tokens": 5,
        "output_tokens": 5,
    }

    adapted = _adapt_router_result_to_raw(router_result)

    assert adapted["stop_reason"] == "end_turn"


def test_adapt_router_result_preserves_thinking_block():
    router_result = {
        "content": "Result text.",
        "thinking": "Reasoning content here.",
        "tool_calls": [],
        "input_tokens": 1,
        "output_tokens": 1,
    }

    adapted = _adapt_router_result_to_raw(router_result)

    thinking_blocks = [b for b in adapted["content"] if b["type"] == "thinking"]
    assert len(thinking_blocks) == 1
    assert thinking_blocks[0]["thinking"] == "Reasoning content here."


def test_adapt_router_result_normalizes_missing_tool_input():
    router_result = {
        "content": "",
        "tool_calls": [{"id": "t1", "name": "do_thing", "input": None}],
    }

    adapted = _adapt_router_result_to_raw(router_result)

    tool_blocks = [b for b in adapted["content"] if b["type"] == "tool_use"]
    assert tool_blocks[0]["input"] == {}
    assert adapted["stop_reason"] == "tool_use"
