"""Unit tests for embedding-model filtering in the chat picker (issue #433).

`GET /claude/models` must not offer embedding-only models (e.g.
nomic-embed-text) in the chat picker: they show up in provider discovery
but can't hold a conversation. Filtering uses the registry's `is_embedding`
flag, with a name heuristic as a fallback for ids that carry no live meta.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
# backend/ must be on sys.path too: importing backend.api.claude cascades into
# backend/api/__init__.py which does bare `from api.findings import ...`.
sys.path.insert(0, str(REPO / "backend"))

from backend.api import claude as claude_api  # noqa: E402
from services.model_registry import ModelInfo  # noqa: E402

pytestmark = pytest.mark.unit


def _model(model_id, *, is_embedding=False, provider_type="ollama"):
    return ModelInfo(
        model_id=model_id,
        provider_id=f"{provider_type}-default",
        provider_type=provider_type,
        display_name=model_id,
        context_window=2048,
        input_cost_per_1k=0.0,
        output_cost_per_1k=0.0,
        supports_tools=False,
        supports_thinking=False,
        supports_vision=False,
        is_embedding=is_embedding,
    )


class _FakeRegistry:
    def __init__(self, models):
        self._models = models

    async def list_available_models(self):
        return list(self._models)


async def test_get_models_excludes_embedding_by_flag(monkeypatch):
    reg = _FakeRegistry(
        [_model("llama3.1:8b"), _model("nomic-embed-text:latest", is_embedding=True)]
    )
    monkeypatch.setattr(claude_api, "get_registry", lambda: reg)
    out = await claude_api.get_models()
    ids = [m["id"] for m in out["models"]]
    assert ids == ["llama3.1:8b"]


async def test_get_models_excludes_embedding_by_name_when_flag_absent(monkeypatch):
    # No live meta → is_embedding flag is False; the name heuristic must still
    # keep nomic-embed-text out of the chat picker.
    reg = _FakeRegistry(
        [_model("qwen2.5:14b"), _model("nomic-embed-text:latest", is_embedding=False)]
    )
    monkeypatch.setattr(claude_api, "get_registry", lambda: reg)
    out = await claude_api.get_models()
    ids = [m["id"] for m in out["models"]]
    assert "qwen2.5:14b" in ids
    assert "nomic-embed-text:latest" not in ids


async def test_get_models_keeps_chat_models(monkeypatch):
    reg = _FakeRegistry([_model("qwen2.5:14b"), _model("llama3.1:8b")])
    monkeypatch.setattr(claude_api, "get_registry", lambda: reg)
    out = await claude_api.get_models()
    ids = {m["id"] for m in out["models"]}
    assert ids == {"qwen2.5:14b", "llama3.1:8b"}
