"""When a custom agent's model strings change, and when they do not."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.agents.builtins import blank_model  # noqa: E402
from core.agents.custom_agent_service import (  # noqa: E402
    model_pair,
    model_strings_changed,
)

pytestmark = pytest.mark.unit


def test_blank_model_strings_store_as_null():
    assert blank_model(None) is None
    assert blank_model("") is None
    assert blank_model("   ") is None
    assert model_pair("  qwen2.5 ", "") == {
        "model": "qwen2.5",
        "fallback_model": None,
    }


def test_unchanged_model_strings_write_no_audit_row():
    same = model_pair("qwen2.5", None)
    assert model_strings_changed(same, dict(same)) is False
    assert model_strings_changed(None, model_pair("  ", None)) is False
    assert model_strings_changed(model_pair(None, None), None) is False


def test_a_changed_model_string_is_audited():
    assert model_strings_changed(None, model_pair("qwen2.5", None)) is True
    assert (
        model_strings_changed(
            model_pair("qwen2.5", None), model_pair("qwen2.5", "mistral")
        )
        is True
    )
    assert model_strings_changed(model_pair("qwen2.5", None), None) is True
