"""Unit tests for Anthropic prompt caching markers (GH #84 PR-C).

Covers ``ClaudeService._apply_prompt_cache_controls`` — the pure helper that
tags the system prompt and the last tool definition with
``cache_control: {"type": "ephemeral"}`` so Anthropic serves repeated
prefixes from cache at ~10% the input-token cost.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

pytestmark = pytest.mark.unit


def _apply(kwargs):
    """Shorthand — imports lazily so test collection doesn't need the module."""
    from services.claude_service import ClaudeService

    ClaudeService._apply_prompt_cache_controls(kwargs)
    return kwargs


class TestSystemPromptCaching:
    def test_string_system_is_converted_to_blocks(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE_ENABLED", raising=False)
        kw = {"system": "You are a SOC analyst."}
        _apply(kw)
        assert kw["system"] == [
            {
                "type": "text",
                "text": "You are a SOC analyst.",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def test_empty_string_system_is_left_alone(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE_ENABLED", raising=False)
        kw = {"system": ""}
        _apply(kw)
        assert kw["system"] == ""

    def test_missing_system_is_a_no_op(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE_ENABLED", raising=False)
        kw: dict = {}
        _apply(kw)
        assert "system" not in kw

    def test_pre_blocked_system_gets_trailing_cache_marker(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE_ENABLED", raising=False)
        kw = {
            "system": [
                {"type": "text", "text": "common preamble"},
                {"type": "text", "text": "per-call context"},
            ]
        }
        _apply(kw)
        assert kw["system"][0] == {"type": "text", "text": "common preamble"}
        assert kw["system"][1]["cache_control"] == {"type": "ephemeral"}
        assert kw["system"][1]["text"] == "per-call context"

    def test_pre_tagged_system_is_not_retagged(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE_ENABLED", raising=False)
        kw = {
            "system": [
                {
                    "type": "text",
                    "text": "already cached",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        }
        _apply(kw)
        # Exactly one block, unchanged.
        assert kw["system"] == [
            {
                "type": "text",
                "text": "already cached",
                "cache_control": {"type": "ephemeral"},
            }
        ]


class TestToolCaching:
    def test_last_tool_gets_cache_marker(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE_ENABLED", raising=False)
        tools = [
            {"name": "alpha", "description": "x", "input_schema": {}},
            {"name": "beta", "description": "y", "input_schema": {}},
            {"name": "gamma", "description": "z", "input_schema": {}},
        ]
        kw = {"tools": tools}
        _apply(kw)
        assert kw["tools"][0] == {"name": "alpha", "description": "x", "input_schema": {}}
        assert kw["tools"][1] == {"name": "beta", "description": "y", "input_schema": {}}
        assert kw["tools"][2]["cache_control"] == {"type": "ephemeral"}
        assert kw["tools"][2]["name"] == "gamma"

    def test_empty_tools_list_untouched(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE_ENABLED", raising=False)
        kw = {"tools": []}
        _apply(kw)
        assert kw["tools"] == []

    def test_missing_tools_is_a_no_op(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE_ENABLED", raising=False)
        kw = {"system": "hello"}
        _apply(kw)
        assert "tools" not in kw


class TestKillSwitch:
    def test_false_disables_caching(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_PROMPT_CACHE_ENABLED", "false")
        kw = {
            "system": "big prompt",
            "tools": [{"name": "x", "input_schema": {}}],
        }
        _apply(kw)
        # System stays a string, tools stay untagged.
        assert kw["system"] == "big prompt"
        assert "cache_control" not in kw["tools"][0]

    @pytest.mark.parametrize("val", ["0", "no", "FALSE"])
    def test_various_falsy_values_disable(self, monkeypatch, val):
        monkeypatch.setenv("ANTHROPIC_PROMPT_CACHE_ENABLED", val)
        kw = {"system": "s"}
        _apply(kw)
        assert kw["system"] == "s"

    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE_ENABLED", raising=False)
        kw = {"system": "s"}
        _apply(kw)
        assert isinstance(kw["system"], list)
